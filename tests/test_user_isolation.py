"""Cada usuario ve solo sus campañas, leads y empresas; el administrador ve todo."""
import importlib
import os
from pathlib import Path
import shutil
import unittest
import uuid
from unittest.mock import patch

from app import auth, db


LEAD = {"source": "google_maps", "name": "Café Central", "address": "Calle 1 # 2-3",
        "city": "Bogotá", "phone": "+57 300 000 0000", "website": "https://cafecentral.co"}


class UserIsolationTests(unittest.TestCase):
    def setUp(self):
        self.old_path = db.DB_PATH
        self.folder = Path(__file__).parent / (".test-" + uuid.uuid4().hex)
        self.folder.mkdir()
        db.DB_PATH = self.folder / "iso.db"
        db.init()
        with patch.dict(os.environ, {"ADMIN_PASSWORD": "test-password-123"}):
            self.main = importlib.import_module("app.main")
            auth.init()
        from fastapi.testclient import TestClient
        self.TestClient = TestClient
        self.admin = self.login("admin", "test-password-123")
        for name in ("ana", "beto"):
            r = self.admin.post("/api/users", json={"username": name, "password": name + "-clave-123"})
            self.assertEqual(r.status_code, 200, r.text)
        self.ana = self.login("ana", "ana-clave-123")
        self.beto = self.login("beto", "beto-clave-123")
        self.ana_cid = self.campaign(self.ana, "Campaña de Ana")
        self.beto_cid = self.campaign(self.beto, "Campaña de Beto")
        self.ana_lead = db.insert("leads", {**LEAD, "campaign_id": self.ana_cid, "dedupe_key": "maps:cafe"})
        self.beto_lead = db.insert("leads", {**LEAD, "campaign_id": self.beto_cid, "dedupe_key": "maps:cafe"})

    def tearDown(self):
        for c in (self.admin, self.ana, self.beto):
            c.close()
        db.DB_PATH = self.old_path
        shutil.rmtree(self.folder)

    def login(self, user, pw):
        client = self.TestClient(self.main.app)
        r = client.post("/api/login", json={"username": user, "password": pw})
        self.assertEqual(r.status_code, 200, r.text)
        return client

    def campaign(self, client, name):
        r = client.post("/api/campaigns", json={"name": name, "keywords": ["café"], "locations": ["Bogotá"],
                                                "sources": ["google_maps"], "options": {"maps_mode": "web"}, "start": False})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["id"]

    def company_of(self, client, lead_id):
        items = client.get("/api/leads").json()["items"]
        return next(i["company_id"] for i in items if i["id"] == lead_id)

    def test_each_user_lists_only_own_campaigns_and_leads(self):
        self.assertEqual([c["id"] for c in self.ana.get("/api/campaigns").json()], [self.ana_cid])
        self.assertEqual([l["id"] for l in self.ana.get("/api/leads").json()["items"]], [self.ana_lead])
        self.assertEqual([l["id"] for l in self.beto.get("/api/leads").json()["items"]], [self.beto_lead])
        # Pedir explícitamente la campaña ajena no devuelve sus leads.
        self.assertEqual(self.ana.get(f"/api/leads?campaign_id={self.beto_cid}").json()["total"], 0)

    def test_admin_sees_everything_with_owner(self):
        rows = {c["id"]: c["owner_name"] for c in self.admin.get("/api/campaigns").json()}
        self.assertEqual(rows, {self.ana_cid: "ana", self.beto_cid: "beto"})
        self.assertEqual(self.admin.get("/api/leads").json()["total"], 2)
        self.assertEqual(self.admin.get(f"/api/campaigns/{self.beto_cid}").status_code, 200)

    def test_cannot_read_or_change_other_users_campaign(self):
        cid = self.beto_cid
        self.assertEqual(self.ana.get(f"/api/campaigns/{cid}").status_code, 404)
        self.assertEqual(self.ana.post(f"/api/campaigns/{cid}/start").status_code, 404)
        self.assertEqual(self.ana.post(f"/api/campaigns/{cid}/stop").status_code, 404)
        self.assertEqual(self.ana.delete(f"/api/campaigns/{cid}").status_code, 404)
        self.assertEqual(self.ana.get(f"/api/export?campaign_id={cid}&fmt=csv").status_code, 404)
        self.assertIsNotNone(db.one("SELECT id FROM campaigns WHERE id=?", (cid,)))

    def test_cannot_read_or_change_other_users_lead(self):
        lid = self.beto_lead
        self.assertEqual(self.ana.patch(f"/api/leads/{lid}", json={"notes": "x"}).status_code, 404)
        for kind in ("website", "linkedin", "instagram"):
            self.assertEqual(self.ana.post(f"/api/leads/{lid}/review-{kind}").status_code, 404)
        self.assertEqual(db.one("SELECT notes FROM leads WHERE id=?", (lid,))["notes"], "")
        own = self.ana.patch(f"/api/leads/{self.ana_lead}", json={"notes": "llamar"})
        self.assertEqual(own.status_code, 200, own.text)

    def test_export_contains_only_own_leads(self):
        def rows(client):  # filas de datos, sin la cabecera
            return len(client.get("/api/export?fmt=csv").text.strip().splitlines()) - 1
        self.assertEqual(rows(self.ana), 1)
        self.assertEqual(rows(self.beto), 1)
        self.assertEqual(rows(self.admin), 2)

    def test_same_business_becomes_separate_companies_per_user(self):
        ana_co, beto_co = self.company_of(self.ana, self.ana_lead), self.company_of(self.beto, self.beto_lead)
        self.assertNotEqual(ana_co, beto_co)
        self.assertEqual([c["id"] for c in self.ana.get("/api/companies").json()["items"]], [ana_co])
        self.assertEqual(self.ana.get(f"/api/companies/{beto_co}").status_code, 404)
        self.assertEqual(self.ana.patch(f"/api/companies/{beto_co}", json={"name": "Hack"}).status_code, 404)
        self.assertEqual(self.ana.post(f"/api/companies/{beto_co}/contacts", json={"name": "X"}).status_code, 404)
        self.assertEqual(self.ana.post(f"/api/companies/{beto_co}/review-decision-makers").status_code, 404)
        # Ni siquiera el admin puede mezclar registros de dos usuarios en una empresa.
        r = self.admin.post(f"/api/leads/{self.ana_lead}/company", json={"company_id": beto_co})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.ana.post(f"/api/leads/{self.ana_lead}/company", json={"company_id": beto_co}).status_code, 404)

    def test_contact_decisions_are_scoped(self):
        beto_co = self.company_of(self.beto, self.beto_lead)
        contact = self.beto.post(f"/api/companies/{beto_co}/contacts", json={"name": "Laura", "role": "Gerente"}).json()
        self.assertEqual(self.ana.patch(f"/api/company-contacts/{contact['id']}", json={"relationship": "rejected"}).status_code, 404)
        self.assertEqual(self.beto.patch(f"/api/company-contacts/{contact['id']}", json={"relationship": "confirmed"}).status_code, 200)

    def test_admin_resets_password_and_closes_sessions(self):
        uid = db.one("SELECT id FROM users WHERE username='ana'")["id"]
        self.assertEqual(self.ana.post(f"/api/users/{uid}/password", json={"password": "nueva-clave-1"}).status_code, 403)
        self.assertEqual(self.admin.post(f"/api/users/{uid}/password", json={"password": "corta"}).status_code, 400)
        self.assertEqual(self.admin.post(f"/api/users/{uid}/password", json={"password": "nueva-clave-1"}).status_code, 200)
        self.assertEqual(self.ana.get("/api/campaigns").status_code, 401)
        self.login("ana", "nueva-clave-1").close()

    def test_deleting_user_hands_data_to_admin(self):
        uid = db.one("SELECT id FROM users WHERE username='beto'")["id"]
        admin_id = db.one("SELECT id FROM users WHERE username='admin'")["id"]
        self.company_of(self.beto, self.beto_lead)
        self.assertEqual(self.admin.delete(f"/api/users/{uid}").status_code, 200)
        self.assertEqual(db.one("SELECT owner_id FROM campaigns WHERE id=?", (self.beto_cid,))["owner_id"], admin_id)
        self.assertEqual(db.query("SELECT id FROM companies WHERE owner_id=?", (uid,)), [])

    def test_existing_data_without_owner_goes_to_admin(self):
        legacy = db.insert("campaigns", {"name": "Antigua", "keywords": [], "locations": [], "sources": []})
        db.assign_orphans()
        admin_id = db.one("SELECT id FROM users WHERE username='admin'")["id"]
        self.assertEqual(db.one("SELECT owner_id FROM campaigns WHERE id=?", (legacy,))["owner_id"], admin_id)
        self.assertNotIn(legacy, [c["id"] for c in self.ana.get("/api/campaigns").json()])


if __name__ == "__main__":
    unittest.main()
