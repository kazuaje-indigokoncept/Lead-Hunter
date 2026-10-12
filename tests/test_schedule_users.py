"""Campañas por tandas programadas y gestión de usuarios del administrador."""
import asyncio
import importlib
import os
from pathlib import Path
import shutil
import time
import unittest
import uuid
from unittest.mock import AsyncMock, patch

from app import auth, db, jobs


class Base(unittest.TestCase):
    def setUp(self):
        self.old_path = db.DB_PATH
        self.folder = Path(__file__).parent / (".test-" + uuid.uuid4().hex)
        self.folder.mkdir()
        db.DB_PATH = self.folder / "sched.db"
        db.init()
        with patch.dict(os.environ, {"ADMIN_PASSWORD": "test-password-123"}):
            self.main = importlib.import_module("app.main")
            auth.init()
        from fastapi.testclient import TestClient
        self.TestClient = TestClient
        self.clients = []
        self.admin = self.login("admin", "test-password-123")
        jobs.RUNNING.clear(); jobs.BATCH_RUNNING.clear()
        self.sleep = patch.object(jobs.asyncio, "sleep", AsyncMock())
        self.sleep.start()

    def tearDown(self):
        self.sleep.stop()
        for c in self.clients:
            c.close()
        jobs.RUNNING.clear(); jobs.BATCH_RUNNING.clear()
        db.DB_PATH = self.old_path
        shutil.rmtree(self.folder)

    def login(self, user, pw, expect=200):
        client = self.TestClient(self.main.app)
        self.clients.append(client)
        r = client.post("/api/login", json={"username": user, "password": pw})
        self.assertEqual(r.status_code, expect, r.text)
        return client

    def make_user(self, name, admin=False):
        r = self.admin.post("/api/users", json={"username": name, "password": name + "-clave-123", "is_admin": admin})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["id"]

    def campaign(self, client=None, schedule=None, sources=("google_maps",), kws=("café", "pan"), locs=("Quito", "Cuenca")):
        opts = {"maps_mode": "web"}
        if schedule is not None:
            opts["schedule"] = schedule
        r = (client or self.admin).post("/api/campaigns", json={
            "name": "Prueba", "keywords": list(kws), "locations": list(locs),
            "sources": list(sources), "options": opts, "start": False})
        return r

    def camp(self, cid):
        return db.one("SELECT * FROM campaigns WHERE id = ?", (cid,))


class ScheduleTests(Base):
    SCH = {"enabled": True, "interval_minutes": 60, "combos_per_batch": 1}

    def test_schedule_is_validated(self):
        for bad in ({"enabled": True, "interval_minutes": 2, "combos_per_batch": 1},
                    {"enabled": True, "interval_minutes": 60, "combos_per_batch": 0},
                    {"enabled": True, "interval_minutes": 60 * 24 * 8, "combos_per_batch": 1},
                    {"enabled": True, "interval_minutes": "60", "combos_per_batch": 1}):
            self.assertEqual(self.campaign(schedule=bad).status_code, 422, bad)
        self.assertEqual(self.campaign(schedule=self.SCH).status_code, 200)

    def test_batches_advance_one_search_at_a_time_until_done(self):
        cid = self.campaign(schedule=self.SCH).json()["id"]
        info = self.admin.get(f"/api/campaigns/{cid}").json()
        self.assertEqual((info["total_combos"], info["batches_total"]), (4, 4))
        before = time.time()
        asyncio.run(jobs.run_batch(cid))
        c = self.camp(cid)
        self.assertEqual((c["status"], c["batch_cursor"]), ("programada", 1))
        self.assertAlmostEqual(c["progress"], 0.25, places=2)
        # Intervalo de 60 min con ±15 % de variación.
        self.assertTrue(before + 50 * 60 <= c["next_run_at"] <= time.time() + 70 * 60)
        cities = {l["dedupe_key"].split(":")[-1] for l in db.query("SELECT dedupe_key FROM leads WHERE campaign_id=?", (cid,))}
        self.assertEqual(cities, {"Quito"})  # solo la primera búsqueda: 'café en Quito'
        for _ in range(3):
            asyncio.run(jobs.run_batch(cid))
        c = self.camp(cid)
        self.assertEqual((c["status"], c["batch_cursor"], c["progress"], c["next_run_at"]), ("terminada", 4, 1, None))
        self.assertIn("Campaña completada", c["log"][-1])

    def test_batch_enriches_only_its_own_leads(self):
        cid = self.campaign(schedule=self.SCH, sources=("google_maps", "website")).json()["id"]
        other = db.insert("leads", {"campaign_id": cid, "dedupe_key": "manual:x", "source": "manual",
                                    "name": "Otro", "website": "https://otro.test"})
        asyncio.run(jobs.run_batch(cid))
        found = db.query("SELECT * FROM leads WHERE campaign_id=? AND website IS NOT NULL AND id != ?", (cid, other))
        self.assertTrue(found and all(l["enriched"] for l in found))
        self.assertEqual(db.one("SELECT enriched FROM leads WHERE id=?", (other,))["enriched"], 0)

    def test_scheduler_runs_due_batches_one_at_a_time(self):
        a = self.campaign(schedule=self.SCH).json()["id"]
        b = self.campaign(schedule=self.SCH).json()["id"]
        future = self.campaign(schedule=self.SCH).json()["id"]
        db.update("campaigns", a, {"status": "programada", "next_run_at": time.time() - 10})
        db.update("campaigns", b, {"status": "programada", "next_run_at": time.time() - 5})
        db.update("campaigns", future, {"status": "programada", "next_run_at": time.time() + 3600})

        async def scenario():
            self.assertEqual(jobs.scheduler_tick(), a)          # la más antigua primero
            self.assertIsNone(jobs.scheduler_tick())            # no dos tandas a la vez
            await jobs.RUNNING[a]
            self.assertEqual(jobs.scheduler_tick(), b)
            await jobs.RUNNING[b]
            self.assertIsNone(jobs.scheduler_tick())            # la futura aún no vence
        asyncio.run(scenario())
        self.assertEqual(self.camp(a)["batch_cursor"], 1)
        self.assertEqual(self.camp(future)["batch_cursor"], 0)

    def test_errors_back_off_then_stop(self):
        cid = self.campaign(schedule=self.SCH).json()["id"]
        with patch.object(jobs.Runner, "phase_google_maps", AsyncMock(side_effect=RuntimeError("captcha"))):
            asyncio.run(jobs.run_batch(cid))
            c = self.camp(cid)
            self.assertEqual((c["status"], c["batch_cursor"], c["batch_errors"]), ("programada", 0, 1))
            self.assertGreater(c["next_run_at"], time.time() + 100 * 60)   # espera el doble
            asyncio.run(jobs.run_batch(cid)); asyncio.run(jobs.run_batch(cid))
        c = self.camp(cid)
        self.assertEqual((c["status"], c["batch_errors"], c["next_run_at"]), ("error", 3, None))

    def test_pause_and_resume_keep_position(self):
        cid = self.campaign(schedule=self.SCH).json()["id"]
        asyncio.run(jobs.run_batch(cid))
        self.assertEqual(self.admin.post(f"/api/campaigns/{cid}/stop").json(), {"stopped": True})
        self.assertEqual((self.camp(cid)["status"], self.camp(cid)["next_run_at"]), ("pausada", None))

        async def resume():
            self.assertTrue(jobs.start(cid))
            await jobs.RUNNING[cid]
        asyncio.run(resume())
        self.assertEqual((self.camp(cid)["status"], self.camp(cid)["batch_cursor"]), ("programada", 2))

    def test_restart_recovery(self):
        sched = self.campaign(schedule=self.SCH).json()["id"]
        plain = self.campaign().json()["id"]
        for cid in (sched, plain):
            db.update("campaigns", cid, {"status": "en curso"})
        jobs.recover()
        self.assertEqual(self.camp(sched)["status"], "programada")
        self.assertLessEqual(self.camp(sched)["next_run_at"], time.time())
        self.assertEqual(self.camp(plain)["status"], "detenida")
        self.assertIn("reinicio", self.camp(plain)["log"][-1])

    def test_schedule_endpoint(self):
        self.make_user("ana")
        ana = self.login("ana", "ana-clave-123")
        cid = self.campaign().json()["id"]
        self.assertEqual(ana.put(f"/api/campaigns/{cid}/schedule", json=self.SCH).status_code, 404)
        self.assertEqual(self.admin.put(f"/api/campaigns/{cid}/schedule", json={**self.SCH, "interval_minutes": 1}).status_code, 422)
        r = self.admin.put(f"/api/campaigns/{cid}/schedule", json={**self.SCH, "combos_per_batch": 3})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["batches_total"], 2)
        db.update("campaigns", cid, {"status": "programada", "next_run_at": time.time() + 3600})
        self.admin.put(f"/api/campaigns/{cid}/schedule", json={**self.SCH, "enabled": False})
        self.assertEqual(self.camp(cid)["status"], "detenida")

    def test_campaign_without_schedule_runs_complete(self):
        cid = self.campaign().json()["id"]
        asyncio.run(jobs.run_campaign(cid))
        c = self.camp(cid)
        self.assertEqual(c["status"], "terminada")
        cities = {l["dedupe_key"].split(":")[-1] for l in db.query("SELECT dedupe_key FROM leads WHERE campaign_id=?", (cid,))}
        self.assertEqual(cities, {"Quito", "Cuenca"})


class UserAdminTests(Base):
    def test_list_shows_activity(self):
        uid = self.make_user("ana")
        ana = self.login("ana", "ana-clave-123")
        self.campaign(client=ana)
        row = next(u for u in self.admin.get("/api/users").json() if u["id"] == uid)
        self.assertEqual((row["campaign_count"], row["active"], row["open_sessions"]), (1, 1, 1))
        self.assertTrue(row["last_login"])
        self.assertEqual(len(self.admin.get(f"/api/campaigns?owner_id={uid}").json()), 1)
        # El filtro por dueño no amplía lo que ve un usuario normal.
        self.assertEqual(len(ana.get("/api/campaigns?owner_id=1").json()), 1)

    def test_deactivate_blocks_access_immediately(self):
        uid = self.make_user("ana")
        ana = self.login("ana", "ana-clave-123")
        self.assertEqual(self.admin.patch(f"/api/users/{uid}", json={"active": False}).status_code, 200)
        self.assertEqual(ana.get("/api/campaigns").status_code, 401)
        r = self.TestClient(self.main.app); self.clients.append(r)
        resp = r.post("/api/login", json={"username": "ana", "password": "ana-clave-123"})
        self.assertEqual(resp.status_code, 403)
        self.assertIn("desactivado", resp.json()["detail"])
        self.admin.patch(f"/api/users/{uid}", json={"active": True})
        self.login("ana", "ana-clave-123")

    def test_roles_and_safety_rules(self):
        admin_id = db.one("SELECT id FROM users WHERE username='admin'")["id"]
        uid = self.make_user("ana")
        self.assertEqual(self.admin.patch(f"/api/users/{admin_id}", json={"active": False}).status_code, 400)
        self.assertEqual(self.admin.patch(f"/api/users/{admin_id}", json={"is_admin": False}).status_code, 400)
        self.assertEqual(self.admin.patch(f"/api/users/{uid}", json={"is_admin": True}).status_code, 200)
        ana = self.login("ana", "ana-clave-123")
        self.assertEqual(len(ana.get("/api/users").json()), 2)                # ahora es admin
        self.assertEqual(ana.patch(f"/api/users/{admin_id}", json={"is_admin": False}).status_code, 200)
        # Ana es la única admin activa: nadie puede quitarle el rol (ni ella misma).
        self.assertEqual(ana.patch(f"/api/users/{uid}", json={"is_admin": False}).status_code, 400)
        self.assertEqual(ana.patch(f"/api/users/{admin_id}", json={"username": "ana"}).status_code, 400)
        self.assertEqual(ana.patch(f"/api/users/{admin_id}", json={"username": "jefe"}).status_code, 200)

    def test_only_admin_manages_users(self):
        uid = self.make_user("ana")
        ana = self.login("ana", "ana-clave-123")
        self.assertEqual(ana.get("/api/users").status_code, 403)
        self.assertEqual(ana.patch(f"/api/users/{uid}", json={"is_admin": True}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
