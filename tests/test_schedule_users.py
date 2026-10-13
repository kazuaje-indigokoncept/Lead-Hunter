"""Campañas por tandas programadas y gestión de usuarios del administrador."""
import asyncio
import importlib
import os
from pathlib import Path
import shutil
import time
import unittest
import uuid
from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

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


TZ = ZoneInfo("America/Bogota")


def at(hour, minute=0, day=12):
    return datetime(2026, 10, day, hour, minute, tzinfo=TZ).timestamp()


class ScheduleTests(Base):
    SCH = {"enabled": True, "window_start": 8 * 60, "window_end": 18 * 60, "daily_profiles": 100}

    def setUp(self):
        super().setUp()
        # Las pruebas que ejecutan tandas lo hacen como si fuera dentro de la franja.
        self.window = patch.object(jobs, "in_window", return_value=True)
        self.window.start()

    def tearDown(self):
        self.window.stop()
        super().tearDown()

    def sched(self, **kw):
        return {**self.SCH, **kw}

    def test_schedule_is_validated(self):
        for bad in (self.sched(window_start=600, window_end=600), self.sched(window_start=600, window_end=630),
                    self.sched(window_start=605), self.sched(window_end=24 * 60 + 30),
                    self.sched(daily_profiles=0), self.sched(daily_profiles=jobs.DEFAULT_GLOBAL_CAP + 10),
                    self.sched(daily_profiles="100")):
            self.assertEqual(self.campaign(schedule=bad).status_code, 422, bad)
        self.assertEqual(self.campaign(schedule=self.sched(window_start=0, window_end=24 * 60)).status_code, 200)
        db.set_setting("max_profiles_day", "1000")
        self.assertEqual(self.campaign(schedule=self.sched(daily_profiles=800)).status_code, 200)

    def test_next_run_is_random_inside_the_window(self):
        cid = self.campaign(schedule=self.SCH).json()["id"]
        camp = self.camp(cid)
        seen = set()
        for _ in range(40):
            ts = jobs.plan_next(camp, at(6))                         # antes de la franja → hoy 8–18
            t = datetime.fromtimestamp(ts, TZ)
            self.assertEqual(t.day, 12)
            self.assertTrue(8 <= t.hour < 18, t)
            seen.add(round(ts / 60))
        self.assertGreater(len(seen), 10)                             # no siempre a la misma hora
        t = datetime.fromtimestamp(jobs.plan_next(camp, at(19)), TZ)  # después → mañana
        self.assertEqual((t.day, 8 <= t.hour < 18), (13, True))
        t = datetime.fromtimestamp(jobs.plan_next(camp, at(12)), TZ)  # dentro → hoy, después de ahora
        self.assertTrue(at(12) < t.timestamp() < at(18))

    def test_window_crosses_correctly_with_quota_used(self):
        cid = self.campaign(schedule=self.SCH).json()["id"]
        db.update("campaigns", cid, {"daily_date": "2026-10-12", "daily_used": 100})
        with patch.object(jobs, "local_now", side_effect=lambda ts=None: datetime.fromtimestamp(ts if ts is not None else at(10), TZ)):
            t = datetime.fromtimestamp(jobs.plan_next(self.camp(cid), at(10)), TZ)
        self.assertEqual(t.day, 13)                                   # cupo agotado → mañana

    def test_batch_respects_daily_profiles_and_waits_for_tomorrow(self):
        cid = self.campaign(schedule=self.sched(daily_profiles=10)).json()["id"]
        asyncio.run(jobs.run_batch(cid, manual=True))
        c = self.camp(cid)
        self.assertEqual(db.one("SELECT COUNT(*) n FROM leads WHERE campaign_id=?", (cid,))["n"], 10)
        self.assertEqual((c["status"], c["batch_cursor"], c["daily_used"]), ("programada", 1, 10))
        self.assertEqual(c["runs"][-1]["profiles"], 10)
        asyncio.run(jobs.run_batch(cid))                              # cupo agotado: no consulta nada
        c = self.camp(cid)
        self.assertEqual((c["batch_cursor"], c["daily_used"]), (1, 10))
        self.assertEqual(len(c["runs"]), 1)
        self.assertGreater(jobs.local_now(c["next_run_at"]).date(), jobs.local_now().date())
        self.assertIn("Cupo de perfiles de hoy alcanzado", c["log"][-1])
        self.assertEqual(self.admin.post(f"/api/campaigns/{cid}/start").status_code, 409)

    def test_search_cut_by_quota_is_repeated_next_day(self):
        cid = self.campaign(schedule=self.sched(daily_profiles=30)).json()["id"]
        db.update("campaigns", cid, {"options": {**self.camp(cid)["options"], "maps_max_results": 20}})
        asyncio.run(jobs.run_batch(cid, manual=True))
        asyncio.run(jobs.run_batch(cid, manual=True))
        c = self.camp(cid)
        self.assertEqual((c["daily_used"], c["batch_cursor"]), (30, 1))   # 20 + 10 (cortada)
        self.assertEqual([r["status"] for r in c["runs"]], ["ok", "parcial"])

    def test_server_cap_is_shared_by_all_campaigns(self):
        db.set_setting("max_profiles_day", "15")
        a = self.campaign(schedule=self.sched(daily_profiles=10)).json()["id"]
        b = self.campaign(schedule=self.sched(daily_profiles=10)).json()["id"]
        asyncio.run(jobs.run_batch(a, manual=True))
        self.assertEqual(jobs.quota_left(self.camp(b)), 5)
        asyncio.run(jobs.run_batch(b, manual=True))
        self.assertEqual(self.camp(b)["daily_used"], 5)
        self.assertEqual(jobs.server_used_today(), 15)

    def test_outside_window_is_rescheduled_without_scraping(self):
        cid = self.campaign(schedule=self.SCH).json()["id"]
        with patch.object(jobs, "in_window", return_value=False):
            asyncio.run(jobs.run_batch(cid))
        c = self.camp(cid)
        self.assertEqual((c["status"], c["batch_cursor"], c["daily_used"]), ("programada", 0, 0))
        self.assertIn("Fuera de la franja", c["log"][-1])

    def test_new_scheduled_campaign_waits_for_its_window(self):
        r = self.admin.post("/api/campaigns", json={"name": "P", "keywords": ["café"], "locations": ["Quito"],
                                                    "sources": ["google_maps"], "options": {"maps_mode": "web", "schedule": self.SCH}})
        c = self.camp(r.json()["id"])
        self.assertEqual(c["status"], "programada")
        self.assertNotIn(c["id"], jobs.RUNNING)
        t = jobs.local_now(c["next_run_at"])
        self.assertTrue(8 <= t.hour < 18)
        info = self.admin.get(f"/api/campaigns/{c['id']}").json()
        self.assertEqual((info["per_search"], info["days_left"], info["used_today"]), (40, 1, 0))

    def test_campaign_completes_after_all_searches(self):
        cid = self.campaign(schedule=self.SCH).json()["id"]
        for _ in range(4):
            asyncio.run(jobs.run_batch(cid, manual=True))
        c = self.camp(cid)
        self.assertEqual((c["status"], c["batch_cursor"], c["progress"], c["next_run_at"]), ("terminada", 4, 1, None))
        self.assertIn("Campaña completada", c["log"][-1])

    def test_batch_enriches_only_its_own_leads(self):
        cid = self.campaign(schedule=self.SCH, sources=("google_maps", "website")).json()["id"]
        other = db.insert("leads", {"campaign_id": cid, "dedupe_key": "manual:x", "source": "manual",
                                    "name": "Otro", "website": "https://otro.test"})
        asyncio.run(jobs.run_batch(cid, manual=True))
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
            self.assertEqual(jobs.scheduler_tick(), a)
            self.assertIsNone(jobs.scheduler_tick())
            await jobs.RUNNING[a]
            self.assertEqual(jobs.scheduler_tick(), b)
            await jobs.RUNNING[b]
            self.assertIsNone(jobs.scheduler_tick())
        asyncio.run(scenario())
        self.assertEqual(self.camp(a)["batch_cursor"], 1)
        self.assertEqual(self.camp(future)["batch_cursor"], 0)

    def test_errors_back_off_then_stop(self):
        cid = self.campaign(schedule=self.SCH).json()["id"]
        with patch.object(jobs.Runner, "phase_google_maps", AsyncMock(side_effect=RuntimeError("captcha"))):
            asyncio.run(jobs.run_batch(cid, manual=True))
            c = self.camp(cid)
            self.assertEqual((c["status"], c["batch_cursor"], c["batch_errors"]), ("programada", 0, 1))
            self.assertGreaterEqual(c["next_run_at"], time.time() + jobs.ERROR_WAIT - 5)
            self.assertEqual(c["runs"][-1]["status"], "error")
            asyncio.run(jobs.run_batch(cid, manual=True)); asyncio.run(jobs.run_batch(cid, manual=True))
        c = self.camp(cid)
        self.assertEqual((c["status"], c["batch_errors"], c["next_run_at"]), ("error", 3, None))

    def test_pause_and_resume_keep_position(self):
        cid = self.campaign(schedule=self.SCH).json()["id"]
        asyncio.run(jobs.run_batch(cid, manual=True))
        self.assertEqual(self.admin.post(f"/api/campaigns/{cid}/stop").json(), {"stopped": True})
        self.assertEqual((self.camp(cid)["status"], self.camp(cid)["next_run_at"]), ("pausada", None))
        self.assertEqual(self.admin.post(f"/api/campaigns/{cid}/resume").json(), {"scheduled": True})
        c = self.camp(cid)
        self.assertEqual((c["status"], c["batch_cursor"]), ("programada", 1))
        self.assertTrue(8 <= jobs.local_now(c["next_run_at"]).hour < 18)

    def test_restart_recovery(self):
        sched = self.campaign(schedule=self.SCH).json()["id"]
        plain = self.campaign().json()["id"]
        for cid in (sched, plain):
            db.update("campaigns", cid, {"status": "en curso"})
        jobs.recover()
        self.assertEqual(self.camp(sched)["status"], "programada")
        self.assertTrue(8 <= jobs.local_now(self.camp(sched)["next_run_at"]).hour < 18)
        self.assertEqual(self.camp(plain)["status"], "detenida")
        self.assertIn("reinicio", self.camp(plain)["log"][-1])

    def test_schedule_endpoint(self):
        self.make_user("ana")
        ana = self.login("ana", "ana-clave-123")
        cid = self.campaign().json()["id"]
        self.assertEqual(ana.put(f"/api/campaigns/{cid}/schedule", json=self.SCH).status_code, 404)
        self.assertEqual(self.admin.put(f"/api/campaigns/{cid}/schedule", json=self.sched(window_end=8 * 60)).status_code, 422)
        r = self.admin.put(f"/api/campaigns/{cid}/schedule", json=self.sched(window_start=20 * 60, window_end=22 * 60))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["schedule"]["window_start"], 20 * 60)
        db.update("campaigns", cid, {"status": "programada", "next_run_at": time.time() + 3600})
        self.admin.put(f"/api/campaigns/{cid}/schedule", json=self.sched(window_start=20 * 60, window_end=22 * 60))
        self.assertEqual(jobs.local_now(self.camp(cid)["next_run_at"]).hour in (20, 21), True)
        self.admin.put(f"/api/campaigns/{cid}/schedule", json={**self.SCH, "enabled": False})
        self.assertEqual(self.camp(cid)["status"], "detenida")

    def test_settings_validate_cap_and_timezone(self):
        self.assertEqual(self.admin.post("/api/settings", json={"timezone": "Marte/Base"}).status_code, 422)
        self.assertEqual(self.admin.post("/api/settings", json={"max_profiles_day": "3"}).status_code, 422)
        r = self.admin.post("/api/settings", json={"max_profiles_day": "500", "timezone": "America/Mexico_City"})
        self.assertEqual((r.json()["max_profiles_day"], r.json()["timezone"]), ("500", "America/Mexico_City"))
        self.assertEqual(str(jobs.app_tz()), "America/Mexico_City")

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


class DashboardTests(Base):
    def test_scoped_summary(self):
        self.make_user("ana")
        ana = self.login("ana", "ana-clave-123")
        mine = self.campaign(client=ana).json()["id"]
        theirs = self.campaign().json()["id"]
        for cid, n in ((mine, 3), (theirs, 5)):
            for i in range(n):
                db.insert("leads", {"campaign_id": cid, "dedupe_key": f"k{cid}-{i}", "source": "google_maps", "name": f"N{i}",
                                    "category": "Café", "emails": ["info@x.co"] if i == 0 else [], "status": "contactado" if i == 1 else "nuevo"})
        d = ana.get("/api/dashboard").json()
        self.assertFalse(d["admin"])
        self.assertNotIn("master", d)
        self.assertEqual((d["kpis"]["leads_total"], d["kpis"]["leads_today"], d["kpis"]["with_email"]), (3, 3, 1))
        self.assertEqual(len(d["daily"]), 30)
        self.assertEqual(d["daily"][-1]["n"], 3)
        self.assertEqual({p["status"]: p["n"] for p in d["pipeline"]}, {"nuevo": 2, "contactado": 1, "interesado": 0, "descartado": 0})
        a = self.admin.get("/api/dashboard").json()
        self.assertTrue(a["admin"])
        self.assertEqual(a["kpis"]["leads_total"], 8)
        self.assertIn("master", a)
        self.assertEqual(len(a["master"]["growth"]), 30)
        self.assertEqual({t["username"] for t in a["team"]}, {"admin", "ana"})

    def test_upcoming_lists_scheduled_campaigns(self):
        sch = {"enabled": True, "window_start": 480, "window_end": 1080, "daily_profiles": 100}
        r = self.admin.post("/api/campaigns", json={"name": "Prog", "keywords": ["x"], "locations": ["y"], "sources": ["google_maps"],
                                                     "options": {"maps_mode": "web", "schedule": sch}})
        up = self.admin.get("/api/dashboard").json()["upcoming"]
        self.assertEqual([u["id"] for u in up], [r.json()["id"]])
        self.assertTrue(up[0]["next_run_at"])
