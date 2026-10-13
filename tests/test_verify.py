"""Verificación gratuita de correos y teléfonos y nivel de confianza de la base."""
import asyncio
import csv
import importlib
import io
import os
from pathlib import Path
import shutil
import unittest
import uuid
from unittest.mock import patch

from app import auth, db, intelligence, master, verify

DNS = {"cafe.co": "valid_domain", "gmail.com": "valid_domain", "sinmx.co": "no_mail_server", "noexiste.co": "domain_not_found"}


async def fake_dns(domain):
    return DNS.get(domain, "unknown")


class StaticChecks(unittest.TestCase):
    def test_emails_without_network(self):
        cases = {"logo@2x.png": "invalid", "icon@3x.webp": "invalid", "no-es-correo": "invalid",
                 "ventas@mailinator.com": "disposable", "ana@gmial.com": "typo", "user@example.com": "placeholder",
                 "abc123@sentry.wixpress.com": "placeholder", "tucorreo@negocio.co": "placeholder"}
        for email, status in cases.items():
            self.assertEqual(verify.check_email_static(email)[0], status, email)
        self.assertEqual(verify.check_email_static("info@cafe.co"), (None, None))
        r = asyncio.run(verify.check_email("ana@gmial.com"))
        self.assertEqual((r["status"], r["suggestion"]), ("typo", "ana@gmail.com"))

    def test_dns_results(self):
        with patch.object(verify, "_dns_status", fake_dns):
            self.assertEqual(asyncio.run(verify.check_email("Info@Cafe.co"))["status"], "valid_domain")
            self.assertEqual(asyncio.run(verify.check_email("x@sinmx.co"))["status"], "no_mail_server")
            self.assertEqual(asyncio.run(verify.check_email("x@noexiste.co"))["status"], "domain_not_found")

    def test_phones(self):
        co = verify.check_phone("300 123 4567", "CO")
        self.assertEqual((co["status"], co["type"], co["country"], co["e164"]), ("valid", "móvil", "CO", "+573001234567"))
        self.assertEqual(verify.check_phone("(601) 234 5678", "CO")["type"], "fijo")
        ec = verify.check_phone("+593 99 123 4567", "CO")             # el prefijo manda sobre la región
        self.assertEqual((ec["status"], ec["country"]), ("valid", "EC"))
        self.assertEqual(verify.check_phone("123", "CO")["status"], "invalid")
        self.assertEqual(verify.check_phone("abc", "CO")["status"], "unparseable")


class ConfidenceTests(unittest.TestCase):
    def setUp(self):
        self.old_path = db.DB_PATH
        self.folder = Path(__file__).parent / (".test-" + uuid.uuid4().hex)
        self.folder.mkdir()
        db.DB_PATH = self.folder / "verify.db"
        db.init()
        with patch.dict(os.environ, {"ADMIN_PASSWORD": "test-password-123"}):
            self.main = importlib.import_module("app.main")
            auth.init()
        from fastapi.testclient import TestClient
        self.client = TestClient(self.main.app)
        self.client.post("/api/login", json={"username": "admin", "password": "test-password-123"})
        owner = db.one("SELECT id FROM users")["id"]
        self.cid = db.insert("campaigns", {"name": "C", "keywords": ["x"], "locations": ["Bogotá"], "sources": ["google_maps"],
                                           "owner_id": owner, "options": {"region": "CO"}})
        self.dns = patch.object(verify, "_dns_status", fake_dns)
        self.dns.start()

    def tearDown(self):
        self.dns.stop()
        self.client.close()
        db.DB_PATH = self.old_path
        shutil.rmtree(self.folder)

    def lead(self, key, extra_prov=None, **data):
        lead = {"campaign_id": self.cid, "dedupe_key": key, "source": "google_maps", "name": "Café Central", "city": "Bogotá", **data}
        prov = intelligence.initial_provenance(lead, "google_maps_web")
        for field, value in (extra_prov or {}).items():
            prov.setdefault(field, []).append(intelligence.evidence(value, "website", "https://cafe.co/contacto"))
        lead["provenance"] = prov
        lid = db.insert("leads", lead)
        master.sync_lead(db.one("SELECT * FROM leads WHERE id=?", (lid,)))
        return db.one("SELECT * FROM leads WHERE id=?", (lid,))

    def rec(self, mid):
        return db.one("SELECT * FROM master_records WHERE id=?", (mid,))

    def test_well_corroborated_business_is_high(self):
        l = self.lead("gmaps:1", extra_prov={"phones": "+57 300 123 4567"}, phone="300 123 4567", website="https://cafe.co",
                      emails=["info@cafe.co"], business_status="OPERATIONAL", web_audit={"status": "ok", "checked_at": intelligence.now()})
        before = self.rec(l["master_id"])
        self.assertIn("Correos y teléfonos todavía sin verificar", [r["reason"] for r in before["confidence_reasons"]])
        r = self.client.post(f"/api/leads/{l['id']}/verify")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["verification"]["emails"]["info@cafe.co"]["status"], "valid_domain")
        self.assertEqual(r.json()["master_confidence"]["confidence_level"], "alta")
        rec = self.rec(l["master_id"])
        reasons = [x["reason"] for x in rec["confidence_reasons"]]
        self.assertGreaterEqual(rec["confidence"], 70)
        for expected in ("El mismo teléfono aparece en dos fuentes distintas", "Correo del mismo dominio que la web",
                         "Teléfono válido para su país", "Web del negocio revisada y accesible"):
            self.assertIn(expected, reasons)

    def test_bad_contacts_and_closed_business_are_low(self):
        l = self.lead("gmaps:2", phone="123", emails=["logo@2x.png", "x@noexiste.co"], business_status="CLOSED_PERMANENTLY")
        asyncio.run(verify.verify_leads([l["id"]]))
        rec = self.rec(l["master_id"])
        self.assertEqual((rec["confidence"], rec["confidence_level"]), (0, "baja"))
        reasons = [x["reason"] for x in rec["confidence_reasons"]]
        self.assertIn("Los correos encontrados no pasaron la verificación", reasons)
        self.assertIn("Google Maps lo marca como cerrado permanentemente", reasons)

    def test_failed_emails_are_not_exported_and_filters_work(self):
        good = self.lead("gmaps:3", phone="300 123 4567", emails=["info@cafe.co", "ventas@sinmx.co"], website="https://cafe.co")
        weak = self.lead("gmaps:4", name="Otro", phone="999")
        asyncio.run(verify.verify_leads([good["id"], weak["id"]]))
        rows = list(csv.DictReader(io.StringIO(self.client.get("/api/master/export?fmt=csv&sort=confidence").text.lstrip("﻿")), delimiter=";"))
        first = rows[0]
        self.assertEqual(first["Correos de empresa"], "info@cafe.co")
        self.assertIn("móvil", first["Verificación del teléfono"])
        self.assertIn(first["Nivel de confianza"], ("Alta", "Media"))
        high = self.rec(good["master_id"])["confidence"]
        self.assertEqual(self.client.get(f"/api/master/records?min_confidence={high}").json()["total"], 1)
        s = self.client.get("/api/master/stats").json()
        self.assertEqual(s["conf_high"] + s["conf_mid"] + s["conf_low"], 2)
        self.assertEqual(s["unverified"], 0)
        d = self.client.post("/api/master/deliveries", json={"client": "X", "filters": {"min_confidence": high}})
        self.assertEqual(d.json()["record_count"], 1)

    def test_bulk_verification_job(self):
        a = self.lead("gmaps:5", phone="300 123 4567", emails=["info@cafe.co"])
        self.assertEqual(self.client.get("/api/master/stats").json()["unverified"], 1)

        async def scenario():
            job = await master.verify_base()
            self.assertEqual(job["total"], 1)
            while master.VERIFY_JOB["running"]:
                await asyncio.sleep(0.01)
        asyncio.run(scenario())
        self.assertEqual(master.VERIFY_JOB["done"], 1)
        self.assertTrue(db.one("SELECT verification FROM leads WHERE id=?", (a["id"],))["verification"]["checked_at"])
        self.assertEqual(self.client.get("/api/master/stats").json()["unverified"], 0)

    def test_campaign_run_verifies_what_it_found(self):
        from app import jobs
        db.set_setting("demo_mode", "0")

        async def maps(runner, *args):
            runner.save({"dedupe_key": "gmaps:run", "source": "google_maps", "name": "Run Café", "phone": "300 765 4321",
                         "emails": ["hola@cafe.co"]}, "Bogotá")
        with patch.object(jobs.Runner, "phase_google_maps", maps), patch.object(jobs.Runner, "phase_website", lambda *a: asyncio.sleep(0)):
            db.update("campaigns", self.cid, {"sources": ["google_maps"], "options": {"region": "CO", "maps_mode": "web"}})
            asyncio.run(jobs.Runner(self.cid).run())
        lead = db.one("SELECT * FROM leads WHERE dedupe_key='gmaps:run'")
        self.assertEqual(lead["verification"]["emails"]["hola@cafe.co"]["status"], "valid_domain")
        self.assertEqual(lead["verification"]["phones"]["300 765 4321"]["type"], "móvil")
        self.assertIn("Verificados correos y teléfonos de 1 negocio.", self.camp_log())

    def camp_log(self):
        return "\n".join(db.one("SELECT log FROM campaigns WHERE id=?", (self.cid,))["log"])


if __name__ == "__main__":
    unittest.main()
