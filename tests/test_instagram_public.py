import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from app import db, jobs, intelligence
from app.enrich import instagram_public as ig
import test_maps_api

URL = "https://www.instagram.com/negocio/"
HTML = '''<html><meta property="og:title" content="Negocio (@negocio) • Instagram">
<meta property="og:description" content='1,234 Followers, 20 Following, 10 Posts - Negocio on Instagram: "Ventas hola@negocio.test Tel: +57 300 123 4567 https://wa.me/573001234567"'>
<a href="https://negocio.test">Web</a><script>tracking@wrong.test</script></html>'''


class PublicInstagramTests(unittest.IsolatedAsyncioTestCase):
    async def lookup(self, html=HTML, code=200, final=URL, error=None):
        response = httpx.Response(code, text=html, headers={"content-type": "text/html"})
        with patch.object(ig.website, "fetch_page", AsyncMock(return_value=(response, final, error))):
            return await ig.lookup(None, URL)

    async def test_metadata_contacts_counts_and_source(self):
        data = await self.lookup()
        self.assertEqual(data["instagram_data"]["status"], "public_page")
        self.assertEqual(data["ig_followers"], 1234)
        self.assertIn("Ventas", data["ig_bio"])
        self.assertEqual(data["emails"], ["hola@negocio.test"])
        self.assertEqual(data["phones"], ["+573001234567"])
        self.assertEqual(data["whatsapp"], "https://wa.me/573001234567")
        self.assertEqual(data["provenance"]["emails"][0]["source"], "instagram_public")
        self.assertEqual(data["provenance"]["emails"][0]["url"], URL)
        self.assertIn("https://negocio.test", data["instagram_data"]["external_links"])

    async def test_generic_shell_login_and_block_are_not_success(self):
        for html, code, final, error, status in (
            ('<title>Instagram</title><p>Log in</p>', 200, URL, None, "unavailable_content"),
            (HTML, 200, 'https://www.instagram.com/accounts/login/', None, "login_required"),
            (HTML + '<p>CAPTCHA</p>', 200, URL, None, "login_required"),
            (HTML, 403, URL, None, "access_restricted"),
            (HTML, 429, URL, None, "access_restricted"),
            (HTML, 404, URL, None, "not_found"),
            (HTML, 200, URL, "blocked_robots", "blocked_robots"),
        ):
            with self.subTest(status=status):
                data = await self.lookup(html, code, final, error)
                self.assertEqual(data["instagram_data"]["status"], status)
                self.assertNotIn("emails", data)

    async def test_abbreviated_count_not_claimed_exact(self):
        data = await self.lookup(HTML.replace("1,234 Followers", "1.2K Followers"))
        self.assertNotIn("ig_followers", data)

    async def test_bio_phone_without_label_and_zero_followers(self):
        data = await self.lookup(HTML.replace("Tel: ", "📲 ").replace("1,234 Followers", "0 Followers"))
        self.assertEqual(data["phones"], ["+573001234567"])
        self.assertEqual(data["ig_followers"], 0)

    async def test_invalid_urls_never_fetched(self):
        for value in ("http://instagram.com/negocio", "https://instagram.com.evil.test/negocio", "https://www.instagram.com/p/xyz", "https://www.instagram.com/accounts/", "https://www.instagram.com:bad/negocio", "https://127.0.0.1/negocio"):
            with patch.object(ig.website, "fetch_page", AsyncMock()) as fetch:
                data = await ig.lookup(None, value)
                self.assertEqual(data["instagram_data"]["status"], "invalid_url")
                fetch.assert_not_called()

    async def test_actual_fetch_honors_robots(self):
        requests = []
        def transport(request):
            requests.append(str(request.url))
            return httpx.Response(200, text="User-agent: *\nDisallow: /", headers={"content-type": "text/plain"})
        with patch.object(ig.website, "public_url", AsyncMock(return_value=True)):
            async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
                data = await ig.lookup(client, URL)
        self.assertEqual(data["instagram_data"]["status"], "blocked_robots")
        self.assertEqual(requests, ["https://www.instagram.com/robots.txt"])


class InstagramApiTests(unittest.TestCase):
    setUp = test_maps_api.CampaignApiTests.setUp
    tearDown = test_maps_api.CampaignApiTests.tearDown

    def seed(self):
        self.body.update(sources=["instagram"], options={"instagram_mode": "web"})
        cid = self.client.post("/api/campaigns", json=self.body).json()["id"]
        lid = db.insert("leads", {"campaign_id": cid, "dedupe_key": "ig", "source": "rues", "name": "Negocio",
                                "instagram": URL, "emails": ["previo@negocio.test"], "ig_followers": 42})
        db.set_setting("demo_mode", "0")
        return cid, lid

    def test_review_without_credentials_merges_and_preserves_profile(self):
        cid, lid = self.seed()
        data = {"emails": ["nuevo@negocio.test"], "ig_followers": 1234, "instagram_data": {"status": "public_page", "url": URL},
                "provenance": {"emails": [intelligence.evidence("nuevo@negocio.test", "instagram_public", URL)]}}
        with patch.object(jobs.instagram_public, "lookup", AsyncMock(return_value=data)) as lookup:
            response = self.client.post(f"/api/leads/{lid}/review-instagram?sales_profile=social")
        self.assertEqual(response.status_code, 200)
        row = response.json()
        self.assertEqual(row["sales_profile"], "social")
        self.assertEqual(set(row["emails"]), {"previo@negocio.test", "nuevo@negocio.test"})
        self.assertEqual(row["ig_followers"], 1234)
        lookup.assert_awaited_once()
        self.assertIn("Revisión Instagram", self.client.get("/api/export?fmt=csv").text)

    def test_failed_review_preserves_previous_contacts_and_metrics(self):
        _, lid = self.seed()
        with patch.object(jobs.instagram_public, "lookup", AsyncMock(return_value={"instagram_data": {"status": "blocked_robots"}})):
            row = self.client.post(f"/api/leads/{lid}/review-instagram").json()
        self.assertEqual(row["ig_followers"], 42)
        self.assertEqual(row["emails"], ["previo@negocio.test"])
        self.assertEqual(row["instagram_data"]["status"], "blocked_robots")

    def test_profile_changes_clear_metrics_and_invalid_modes_rejected(self):
        _, lid = self.seed()
        row = self.client.patch(f"/api/leads/{lid}", json={"instagram": "https://instagram.com/otro/"}).json()
        self.assertIsNone(row["ig_followers"])
        self.assertEqual(row["instagram_data"], {})
        self.assertEqual(row["provenance"]["instagram"][-1]["source"], "manual")
        self.assertEqual(self.client.post(f"/api/leads/{lid}/review-instagram?mode=bad").status_code, 422)
        self.assertEqual(self.client.post(f"/api/leads/{lid}/review-instagram?mode=api").status_code, 400)
        self.body["options"] = {"instagram_mode": "bad"}
        self.assertEqual(self.client.post("/api/campaigns", json=self.body).status_code, 422)
        self.assertEqual(self.client.patch(f"/api/leads/{lid}", json={"instagram": "https://instagram.com/p/x"}).status_code, 422)

    def test_campaign_runs_website_discovery_then_public_lookup_without_token(self):
        cid, lid = self.seed()
        events = []
        async def web(runner, *args):
            events.append("web")
        async def public(*args):
            events.append("ig")
            return {"instagram_data": {"status": "public_page"}}
        with patch.object(jobs.Runner, "phase_website", web), patch.object(jobs.instagram_public, "lookup", public), patch.object(jobs.asyncio, "sleep", AsyncMock()):
            asyncio.run(jobs.Runner(cid).run())
        self.assertEqual(events, ["web", "ig"])
        self.assertEqual(db.one("SELECT * FROM campaigns WHERE id=?", (cid,))["status"], "terminada")
        self.assertEqual(db.one("SELECT * FROM leads WHERE id=?", (lid,))["instagram_data"]["status"], "public_page")

    def test_api_mode_keeps_official_path_and_demo_is_labeled(self):
        _, lid = self.seed()
        lead = db.one("SELECT * FROM leads WHERE id=?", (lid,))
        with patch.object(jobs.instagram, "lookup", AsyncMock(return_value={"ig_followers": 20, "ig_bio": "Oficial"})) as lookup:
            row = asyncio.run(jobs.enrich_instagram(None, lead, mode="api", token="test", ig_id="123"))
        lookup.assert_awaited_once_with(None, "123", "test", "negocio")
        self.assertEqual(row["instagram_data"]["status"], "api_success")
        self.assertEqual(row["provenance"]["ig_followers"][-1]["source"], "instagram_api")
        row = asyncio.run(jobs.enrich_instagram(None, row, demo=True))
        self.assertEqual(row["instagram_data"]["status"], "demo")
        self.assertEqual(row["provenance"]["ig_followers"][-1]["verification"], "demo")
