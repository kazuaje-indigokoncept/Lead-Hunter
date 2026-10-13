import importlib
import os
from pathlib import Path
import shutil
import unittest
import uuid
from unittest.mock import patch

from app import auth, db


class CampaignApiTests(unittest.TestCase):
    def setUp(self):
        self.old_path = db.DB_PATH
        self.folder = Path(__file__).parent / (".test-" + uuid.uuid4().hex)
        self.folder.mkdir()
        db.DB_PATH = self.folder / "api.db"
        db.init()
        with patch.dict(os.environ, {"ADMIN_PASSWORD": "test-password-123"}):
            self.main = importlib.import_module("app.main")
            auth.init()
        from fastapi.testclient import TestClient
        self.client = TestClient(self.main.app)
        self.client.post("/api/login", json={"username": "admin", "password": "test-password-123"})
        self.body = {"name": "Prueba", "keywords": ["cafés"], "locations": ["Bogotá"],
                     "sources": ["google_maps", "website"], "options": {"maps_mode": "web"}, "start": False}

    def tearDown(self):
        self.client.close()
        db.DB_PATH = self.old_path
        shutil.rmtree(self.folder)

    def test_web_estimate_has_no_google_api_cost(self):
        response = self.client.post("/api/estimate", json=self.body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["max_calls"], 0)
        self.assertEqual(response.json()["max_results"], 40)
        self.assertEqual(response.json()["max_cost_usd"], 0)

    def test_login_page_loads_local_tailwind_styles_before_authentication(self):
        self.client.cookies.clear()
        page = self.client.get('/')
        self.assertEqual(page.status_code, 200)
        self.assertIn('/static/app.css?v=8', page.text)
        css = self.client.get('/static/app.css')
        self.assertEqual(css.status_code, 200)
        self.assertIn('text/css', css.headers['content-type'])
        self.assertIn('tailwindcss v4.3.0', css.text)
        self.assertEqual(self.client.get('/api/companies').status_code, 401)

    def test_api_estimate_keeps_legacy_behavior(self):
        self.body["options"] = {}
        response = self.client.post("/api/estimate", json=self.body)
        self.assertEqual(response.json()["max_calls"], 3)
        self.assertEqual(response.json()["max_results"], 60)

    def test_create_without_key_and_persist_mode(self):
        db.set_setting("demo_mode", "0")
        response = self.client.post("/api/campaigns", json=self.body)
        self.assertEqual(response.status_code, 200)
        campaign = self.client.get(f'/api/campaigns/{response.json()["id"]}').json()
        self.assertEqual(campaign["options"]["maps_mode"], "web")
        self.assertEqual(db.get_setting("google_api_key"), "")

    def test_invalid_limits_and_modes_are_rejected(self):
        for options in ({"maps_mode": "other"}, {"maps_max_results": 0}, {"maps_max_results": 121},
                        {"maps_max_results": 2.5}, {"maps_delay": 0}, {"max_pages": 99}, {"min_rating": 10}):
            with self.subTest(options=options):
                self.body["options"] = options
                self.assertEqual(self.client.post("/api/campaigns", json=self.body).status_code, 422)


if __name__ == "__main__":
    unittest.main()
