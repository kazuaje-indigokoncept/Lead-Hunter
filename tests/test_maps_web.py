import asyncio
import os
from pathlib import Path
import shutil
import unittest
import uuid
from unittest.mock import AsyncMock, patch

from app import db, jobs
from app.sources import maps_web


def example(name="Negocio", **kwargs):
    return maps_web.normalize({"name": name, "address": "Cra 7, Bogotá", "maps_url":
        "https://www.google.com/maps/place/Negocio/data=!1sChIJtest", **kwargs})


class NormalizationTests(unittest.TestCase):
    def test_contact_data_and_spanish_numbers(self):
        lead = example(phone="+57 300 123 4567", rating="4,7 estrellas", reviews="1.234 reseñas",
                       website="https://www.google.com/url?q=https%3A%2F%2Fnegocio.test")
        self.assertEqual(lead["dedupe_key"], "gmaps:ChIJtest")
        self.assertEqual(lead["rating"], 4.7)
        self.assertEqual(lead["reviews"], 1234)
        self.assertEqual(lead["website"], "https://negocio.test")
        self.assertEqual(lead["whatsapp"], "https://wa.me/573001234567")

    def test_missing_fields_and_local_phone(self):
        lead = example(phone="300 123 4567")
        self.assertIsNone(lead["whatsapp"])
        self.assertIsNone(lead["rating"])
        self.assertIsNone(lead["reviews"])
        self.assertIsNone(lead["website"])

    def test_stable_fallback_and_closed(self):
        lead = maps_web.normalize({"name": "A", "address": "B", "status": "Cerrado permanentemente"})
        same = maps_web.normalize({"name": "a", "address": "b"})
        self.assertEqual(lead["dedupe_key"], same["dedupe_key"])
        self.assertEqual(lead["business_status"], "CLOSED_PERMANENTLY")


class FakeBrowser:
    closed = False
    blocked = False
    stop = False
    async def __aenter__(self):
        return self
    async def __aexit__(self, *_):
        FakeBrowser.closed = True
    def __init__(self, log=None, check_stop=None):
        self.check_stop = check_stop
    async def search(self, *args, **kwargs):
        yield example(website="https://negocio.test")
        if FakeBrowser.stop:
            jobs.stop(next(iter(jobs.RUNNING)))
            self.check_stop()
        if FakeBrowser.blocked:
            raise maps_web.MapsBlockedError("captcha")


class CampaignTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.previous = db.DB_PATH
        self.folder = Path(__file__).parent / (".test-" + uuid.uuid4().hex)
        self.folder.mkdir()
        db.DB_PATH = self.folder / "test.db"
        db.init()
        db.set_setting("demo_mode", "0")
        FakeBrowser.closed = FakeBrowser.blocked = FakeBrowser.stop = False

    def tearDown(self):
        db.DB_PATH = self.previous
        shutil.rmtree(self.folder)
        jobs.RUNNING.clear()
        jobs.STOP_REQUESTED.clear()

    def campaign(self, options=None, sources=None):
        return db.insert("campaigns", {"name": "Prueba", "keywords": ["cafés"], "locations": ["Bogotá"],
            "sources": sources or ["google_maps"], "options": options or {"maps_mode": "web"}})

    async def test_no_key_enrichment_deduplication_and_zero_api_calls(self):
        cid = self.campaign(sources=["google_maps", "website"])
        with patch.object(maps_web, "MapsBrowser", FakeBrowser), patch.object(
            jobs.website, "enrich", AsyncMock(return_value={"emails": ["hola@negocio.test"]})
        ), patch.object(jobs.places, "search", AsyncMock(side_effect=AssertionError("API called"))):
            await jobs.Runner(cid).run()
            await jobs.Runner(cid).run()
        leads = db.query("SELECT * FROM leads WHERE campaign_id = ?", (cid,))
        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0]["emails"], ["hola@negocio.test"])
        self.assertEqual(db.one("SELECT * FROM campaigns WHERE id=?", (cid,))["api_calls"], 0)
        self.assertTrue(FakeBrowser.closed)

    async def test_captcha_preserves_saved_leads(self):
        cid = self.campaign()
        FakeBrowser.blocked = True
        with patch.object(maps_web, "MapsBrowser", FakeBrowser), patch("traceback.print_exc"):
            await jobs.run_campaign(cid)
        self.assertEqual(db.one("SELECT * FROM campaigns WHERE id=?", (cid,))["status"], "error")
        self.assertEqual(len(db.query("SELECT * FROM leads")), 1)
        self.assertTrue(FakeBrowser.closed)

    async def test_stop_preserves_saved_leads_and_cleans_running(self):
        cid = self.campaign()
        FakeBrowser.stop = True
        with patch.object(maps_web, "MapsBrowser", FakeBrowser):
            jobs.start(cid)
            await jobs.RUNNING[cid]
        self.assertEqual(db.one("SELECT * FROM campaigns WHERE id=?", (cid,))["status"], "detenida")
        self.assertEqual(len(db.query("SELECT * FROM leads")), 1)
        self.assertNotIn(cid, jobs.RUNNING)
        self.assertTrue(FakeBrowser.closed)

    async def test_existing_campaign_defaults_to_api(self):
        cid = self.campaign(options={"max_pages": 1})
        with self.assertRaisesRegex(RuntimeError, "API key"):
            await jobs.Runner(cid).run()

    async def test_web_rues_cross_never_calls_places(self):
        cid = self.campaign(options={"maps_mode": "web", "rues_cross_maps": True}, sources=["rues"])
        db.set_setting("google_api_key", "unused-test-key")
        with patch.object(jobs.rues, "search", AsyncMock(return_value=[])), patch.object(
            jobs.places, "search", AsyncMock(side_effect=AssertionError("API called"))
        ) as api:
            await jobs.Runner(cid).run()
            api.assert_not_called()


@unittest.skipUnless(os.getenv("MAPS_BROWSER_TESTS") == "1", "Requires installed Chromium")
class BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_chromium_extracts_public_dom_and_enforces_limit(self):
        async with maps_web.MapsBrowser() as browser:
            async def route(req):
                if "/maps/search/" in req.request.url:
                    html = '<div role="feed"><a href="https://www.google.com/maps/place/A/data=!1sChIJA">A</a><a href="https://www.google.com/maps/place/A/data=!1sChIJA">A</a><a href="https://www.google.com/maps/place/B/data=!1sChIJB">B</a></div>'
                else:
                    html = '''<main role="main"><h1 class="DUwDvf">Café Prueba</h1>
                    <button jsaction="pane.category">Cafetería</button>
                    <button data-item-id="address" aria-label="Dirección: Calle 10, Bogotá"></button>
                    <button data-item-id="phone:tel:+573001234567" aria-label="Teléfono: +57 300 123 4567"></button>
                    <a data-item-id="authority" href="https://cafe.test">Web</a>
                    <span role="img" aria-label="4,8 estrellas"></span>
                    <button jsaction="pane.reviewChart.moreReviews" aria-label="1.234 reseñas">Reseñas</button></main>'''
                await req.fulfill(body=html, content_type="text/html")
            await browser.context.route("**/*", route)
            rows = [r async for r in browser.search("cafés Bogotá", max_results=1, delay=0)]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["address"], "Calle 10, Bogotá")
            self.assertEqual(rows[0]["name"], "Café Prueba")
            self.assertEqual(rows[0]["rating"], 4.8)
            self.assertEqual(rows[0]["reviews"], 1234)
            self.assertEqual(rows[0]["phone"], "+57 300 123 4567")
            self.assertEqual(rows[0]["website"], "https://cafe.test/")

    async def test_real_browser_detects_captcha(self):
        async with maps_web.MapsBrowser() as browser:
            await browser.context.route("**/*", lambda route: route.fulfill(body="<body>Tráfico inusual</body>", content_type="text/html"))
            with self.assertRaises(maps_web.MapsBlockedError):
                async for _ in browser.search("cafés Bogotá", max_results=1, delay=0):
                    pass


if __name__ == "__main__":
    unittest.main()
