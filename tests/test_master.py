"""Base general: unificación, exclusiones, entregas, datos personales y permisos."""
import csv
import importlib
import io
import os
from pathlib import Path
import shutil
import unittest
import uuid
from unittest.mock import patch

from app import auth, db, master, intelligence


def prov(lead):
    return intelligence.initial_provenance(lead, "google_maps_web")


class MasterTests(unittest.TestCase):
    def setUp(self):
        self.old_path = db.DB_PATH
        self.folder = Path(__file__).parent / (".test-" + uuid.uuid4().hex)
        self.folder.mkdir()
        db.DB_PATH = self.folder / "master.db"
        db.init()
        with patch.dict(os.environ, {"ADMIN_PASSWORD": "test-password-123"}):
            self.main = importlib.import_module("app.main")
            auth.init()
        from fastapi.testclient import TestClient
        self.TestClient = TestClient
        self.clients = []
        self.admin = self.login("admin", "test-password-123")
        self.admin.post("/api/users", json={"username": "ana", "password": "ana-clave-123"})
        self.ana = self.login("ana", "ana-clave-123")
        self.admin_id = db.one("SELECT id FROM users WHERE username='admin'")["id"]
        self.ana_id = db.one("SELECT id FROM users WHERE username='ana'")["id"]
        self.c_admin = self.camp(self.admin_id)
        self.c_ana = self.camp(self.ana_id)

    def tearDown(self):
        for c in self.clients:
            c.close()
        db.DB_PATH = self.old_path
        shutil.rmtree(self.folder)

    def login(self, user, pw):
        client = self.TestClient(self.main.app)
        self.clients.append(client)
        self.assertEqual(client.post("/api/login", json={"username": user, "password": pw}).status_code, 200)
        return client

    def camp(self, owner):
        return db.insert("campaigns", {"name": "C", "keywords": ["x"], "locations": ["Quito"], "sources": ["google_maps"],
                                       "owner_id": owner})

    def lead(self, cid, key, **data):
        lead = {"campaign_id": cid, "dedupe_key": key, "source": data.pop("source", "google_maps"),
                "name": "Café Central", "city": "Quito", **data}
        lead["provenance"] = prov(lead)
        lid = db.insert("leads", lead)
        row = db.one("SELECT * FROM leads WHERE id=?", (lid,))
        master.sync_lead(row)
        return db.one("SELECT * FROM leads WHERE id=?", (lid,))

    def rec(self, mid):
        return db.one("SELECT * FROM master_records WHERE id=?", (mid,))

    def csv_rows(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        return list(csv.DictReader(io.StringIO(response.text.lstrip("﻿")), delimiter=";"))

    # ---------- unificación ----------
    def test_same_place_from_two_users_is_one_record(self):
        a = self.lead(self.c_admin, "gmaps:abc", phone="+57 300 111 2222", emails=["info@cafe.co"])
        b = self.lead(self.c_ana, "gmaps:abc", phone="+57 300 999 8888", emails=["ventas@cafe.co"], website="https://cafe.co")
        self.assertEqual(a["master_id"], b["master_id"])
        r = self.rec(a["master_id"])
        self.assertEqual((r["lead_count"], r["campaign_count"], r["owner_count"]), (2, 2, 2))
        self.assertEqual(set(r["emails"]), {"info@cafe.co", "ventas@cafe.co"})
        self.assertEqual(r["phone"], "+57 300 999 8888")                   # el dato más reciente
        self.assertEqual(set(r["phones"]), {"+57 300 111 2222", "+57 300 999 8888"})
        self.assertEqual(r["website"], "https://cafe.co")
        self.assertGreater(r["quality"], 0)

    def test_other_sources_match_by_domain_or_address(self):
        a = self.lead(self.c_admin, "gmaps:1", website="https://www.cafecentral.co/inicio", address="Av. Amazonas 123")
        b = self.lead(self.c_ana, "rues:17:999", source="rues", website="http://cafecentral.co")
        c = self.lead(self.c_ana, "manual:1", source="manual", address="av amazonas 123")
        self.assertEqual({a["master_id"], b["master_id"], c["master_id"]}, {a["master_id"]})

    def test_chain_with_same_domain_but_other_name_stays_separate(self):
        a = self.lead(self.c_admin, "gmaps:1", website="https://cadena.co")
        b = self.lead(self.c_admin, "gmaps:2", name="Cadena Norte", website="https://cadena.co")
        self.assertNotEqual(a["master_id"], b["master_id"])

    def test_demo_data_never_enters(self):
        lead = {"campaign_id": self.c_admin, "dedupe_key": "gmaps:demo:x:Quito", "source": "google_maps", "name": "Demo"}
        lead["provenance"] = intelligence.initial_provenance(lead, "demo", demo=True)
        lid = db.insert("leads", lead)
        self.assertIsNone(master.sync_lead(db.one("SELECT * FROM leads WHERE id=?", (lid,))))
        self.assertEqual(db.one("SELECT COUNT(*) n FROM master_records")["n"], 0)

    def test_manual_edits_survive_resync_and_campaign_deletion_keeps_record(self):
        a = self.lead(self.c_admin, "gmaps:1")
        mid = a["master_id"]
        self.admin.patch(f"/api/master/records/{mid}", json={"name": "Café Central S.A.S.", "notes": "Cliente potencial"})
        self.assertEqual(self.admin.post("/api/master/resync").status_code, 200)
        self.assertEqual(self.rec(mid)["name"], "Café Central S.A.S.")
        self.admin.delete(f"/api/campaigns/{self.c_admin}")
        self.assertEqual(self.rec(mid)["name"], "Café Central S.A.S.")
        self.assertEqual(self.rec(mid)["notes"], "Cliente potencial")

    def test_lead_edit_in_campaign_updates_base(self):
        a = self.lead(self.c_ana, "gmaps:1")
        r = self.ana.patch(f"/api/leads/{a['id']}", json={"website": "https://nuevo.co"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.rec(a["master_id"])["website"], "https://nuevo.co")

    def test_merge(self):
        a = self.lead(self.c_admin, "gmaps:1", emails=["info@a.co"])
        b = self.lead(self.c_admin, "gmaps:2", name="Cafe Central Quito", emails=["hola@a.co"])
        self.assertNotEqual(a["master_id"], b["master_id"])
        r = self.admin.post(f"/api/master/records/{b['master_id']}/merge", json={"into": a["master_id"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(self.rec(b["master_id"]))
        self.assertEqual(set(self.rec(a["master_id"])["emails"]), {"info@a.co", "hola@a.co"})
        # Una nueva observación de la ficha fusionada cae en el registro unificado.
        c = self.lead(self.c_ana, "gmaps:2", name="Cafe Central Quito")
        self.assertEqual(c["master_id"], a["master_id"])

    # ---------- exclusiones ----------
    def test_exclusion_wipes_contacts_blocks_export_and_reappearances(self):
        a = self.lead(self.c_admin, "gmaps:1", phone="300 111 2222", emails=["info@cafe.co"], website="https://cafe.co")
        other = self.lead(self.c_admin, "gmaps:9", name="Otro", phone="300 555 5555")
        mid = a["master_id"]
        r = self.admin.post(f"/api/master/records/{mid}/exclude", json={"reason": "Solicitud del titular"})
        self.assertEqual(r.status_code, 200, r.text)
        rec = self.rec(mid)
        self.assertEqual((rec["excluded"], rec["phone"], rec["emails"], rec["website"]), (1, None, [], None))
        ids = [row["ID base"] for row in self.csv_rows(self.admin.get("/api/master/export?fmt=csv"))]
        self.assertEqual(ids, [str(other["master_id"])])
        # Aparece de nuevo en otra campaña con otra ficha pero el mismo teléfono: sigue excluido.
        again = self.lead(self.c_ana, "gmaps:nueva", name="Café Central Norte", phone="+57 300 111 2222")
        self.assertEqual(self.rec(again["master_id"])["excluded"], 1)
        # Volver a incluir recupera los datos desde las campañas.
        self.assertEqual(self.admin.post(f"/api/master/records/{mid}/include").status_code, 200)
        self.assertEqual(self.rec(mid)["website"], "https://cafe.co")
        self.assertEqual(self.rec(again["master_id"])["excluded"], 0)

    def test_manual_exclusions_by_domain_and_email(self):
        a = self.lead(self.c_admin, "gmaps:1", website="https://cafe.co")
        b = self.lead(self.c_admin, "gmaps:2", name="Panadería", emails=["Dueno@Pan.co"])
        self.assertEqual(self.admin.post("/api/master/exclusions", json={"kind": "domain", "value": "www.cafe.co"}).status_code, 200)
        self.assertEqual(self.admin.post("/api/master/exclusions", json={"kind": "email", "value": "dueno@pan.co"}).status_code, 200)
        self.assertEqual(self.admin.post("/api/master/exclusions", json={"kind": "phone", "value": "abc"}).status_code, 400)
        self.assertEqual((self.rec(a["master_id"])["excluded"], self.rec(b["master_id"])["excluded"]), (1, 1))
        for ex in self.admin.get("/api/master/exclusions").json():
            self.admin.delete(f"/api/master/exclusions/{ex['id']}")
        self.assertEqual((self.rec(a["master_id"])["excluded"], self.rec(b["master_id"])["excluded"]), (0, 0))

    # ---------- entregas ----------
    def test_deliveries_skip_what_the_client_already_has(self):
        ids = [self.lead(self.c_admin, f"gmaps:{i}", name=f"Negocio {i}", emails=[f"info@n{i}.co"])["master_id"] for i in range(5)]
        first = self.admin.post("/api/master/deliveries", json={"client": "Agencia Uno", "limit": 3, "price": "US$ 30",
                                                                  "filters": {"has_email": True}})
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["record_count"], 3)
        second = self.admin.post("/api/master/deliveries", json={"client": "agencia uno", "filters": {"has_email": True}})
        self.assertEqual(second.json()["record_count"], 2)                       # solo los que faltaban
        third = self.admin.post("/api/master/deliveries", json={"client": "Agencia Uno", "filters": {}})
        self.assertEqual(third.status_code, 400)                                 # ya tiene todo
        other = self.admin.post("/api/master/deliveries", json={"client": "Otra", "filters": {}})
        self.assertEqual(other.json()["record_count"], 5)
        rows = self.csv_rows(self.admin.get(f"/api/master/deliveries/{first.json()['id']}/download?fmt=csv"))
        self.assertEqual(len(rows), 3)
        listed = self.admin.get("/api/master/records?not_delivered_to=Agencia%20Uno").json()
        self.assertEqual(listed["total"], 0)
        self.assertEqual(self.admin.get(f"/api/master/records?delivery_id={first.json()['id']}").json()["total"], 3)
        self.assertEqual(self.admin.get(f"/api/master/records/{ids[0]}").json()["deliveries"][0]["client"] in ("Agencia Uno", "agencia uno", "Otra"), True)
        self.assertEqual(self.admin.delete(f"/api/master/deliveries/{first.json()['id']}").status_code, 200)
        self.assertEqual(self.admin.get("/api/master/records?not_delivered_to=Agencia%20Uno").json()["total"], 3)

    # ---------- datos personales ----------
    def test_personal_data_only_when_requested(self):
        a = self.lead(self.c_admin, "gmaps:1", emails=["info@cafe.co", "juan.perez@cafe.co"])
        cid = db.one("SELECT company_id FROM leads WHERE id=?", (a["id"],))["company_id"]
        if cid is None:
            from app import companies
            cid = companies.attach(db.one("SELECT * FROM leads WHERE id=?", (a["id"],)))
        db.insert("company_contacts", {"company_id": cid, "identity_key": "x", "name": "Laura Gómez", "role": "Gerente",
                                       "relationship": "confirmed", "emails": ["laura@cafe.co"]})
        plain = self.csv_rows(self.admin.get("/api/master/export?fmt=csv"))[0]
        self.assertEqual(plain["Correos de empresa"], "info@cafe.co")
        self.assertNotIn("juan.perez@cafe.co", ";".join(plain.values()))
        self.assertNotIn("Personas de contacto", plain)
        full = self.csv_rows(self.admin.get("/api/master/export?fmt=csv&include_personal=true"))[0]
        self.assertEqual(full["Otros correos (posible dato personal)"], "juan.perez@cafe.co")
        self.assertIn("Laura Gómez", full["Personas de contacto"])
        xlsx = self.admin.get("/api/master/export?fmt=xlsx")
        self.assertEqual(xlsx.status_code, 200)

    def test_email_classification(self):
        for e, kind in (("info@x.co", "empresa"), ("ventas.quito@x.co", "empresa"), ("contacto2@x.co", "empresa"),
                        ("maria.lopez@x.co", "personal"), ("jgarcia@x.co", "personal")):
            self.assertEqual(master.email_kind(e), kind, e)

    # ---------- permisos y consultas ----------
    def test_only_admins(self):
        self.lead(self.c_admin, "gmaps:1")
        for url in ("/api/master/records", "/api/master/stats", "/api/master/export", "/api/master/deliveries",
                    "/api/master/exclusions", "/api/master/facets"):
            self.assertEqual(self.ana.get(url).status_code, 403, url)
        self.assertEqual(self.ana.post("/api/master/resync").status_code, 403)

    def test_filters_and_stats(self):
        self.lead(self.c_admin, "gmaps:1", city="Quito", category="Cafetería", emails=["info@a.co"], whatsapp="https://wa.me/1")
        self.lead(self.c_admin, "gmaps:2", name="Pan", city="Cuenca", category="Panadería", website="https://pan.co")
        q = lambda s: self.admin.get("/api/master/records?" + s).json()["total"]
        self.assertEqual(q(""), 2)
        self.assertEqual(q("city=cuenca"), 1)
        self.assertEqual(q("has_email=true"), 1)
        self.assertEqual(q("has_wa=true&city=Quito"), 1)
        self.assertEqual(q("no_web=true"), 1)
        self.assertEqual(q("updated_days=7"), 2)
        self.assertEqual(q("q=panad"), 1)
        s = self.admin.get("/api/master/stats").json()
        self.assertEqual((s["total"], s["email"], s["web"], s["cities"]), (2, 1, 1, 2))
        f = self.admin.get("/api/master/facets").json()
        self.assertEqual({c["value"] for c in f["cities"]}, {"Quito", "Cuenca"})


if __name__ == "__main__":
    unittest.main()
