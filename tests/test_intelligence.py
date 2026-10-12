import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app import intelligence
from app.enrich import website, linkedin


class IntelligenceTests(unittest.TestCase):
    def test_unknown_is_not_absence_or_opportunity(self):
        result = intelligence.analyze({"name": "Negocio", "source": "rues"}, "web")
        self.assertFalse(result["opportunities"])
        self.assertTrue(all(t["state"] == "unknown" for t in result["tags"]))

    def test_missing_maps_website_prioritized_for_design(self):
        lead = {"name": "Negocio", "phone": "+573001234567", "reviews": 50}
        lead["provenance"] = intelligence.initial_provenance(lead, "google_maps_web")
        design = intelligence.analyze(lead, "web")
        social = intelligence.analyze(lead, "social")
        self.assertGreater(design["opportunity_score"], social["opportunity_score"])
        self.assertEqual(sum(b["points"] for b in design["score_breakdown"]), design["opportunity_score"])
        self.assertIn("no_website", [o["key"] for o in design["opportunities"]])

    def test_failed_crawl_does_not_invent_missing_socials(self):
        lead = {"website": "https://negocio.test", "web_audit": {"status": "network_error"}}
        result = intelligence.analyze(lead, "social")
        self.assertFalse(result["opportunities"])
        self.assertEqual(next(t for t in result["tags"] if t["key"] == "instagram")["state"], "unknown")

    def test_audit_signals_and_seo_profile(self):
        lead = {"website": "https://negocio.test", "web_audit": {"status": "ok", "has_contact_form": False,
            "has_meta_description": False, "has_title": False, "has_viewport": True, "https": True}}
        result = intelligence.analyze(lead, "seo")
        self.assertEqual(result["opportunity_score"], 45)
        self.assertEqual({o["key"] for o in result["relevant_opportunities"]}, {"has_meta_description", "has_title"})

    def test_inferred_whatsapp_not_published(self):
        lead = {"whatsapp": "https://wa.me/573001234567"}
        lead["provenance"] = intelligence.initial_provenance(lead, "google_maps_api")
        result = intelligence.analyze(lead)
        self.assertEqual(next(t for t in result["tags"] if t["key"] == "whatsapp")["state"], "inferred")
        lead["provenance"] = intelligence.merge_provenance(lead["provenance"], {"whatsapp": [intelligence.evidence(lead["whatsapp"], "website", "https://negocio.test/contacto")]})
        self.assertEqual(next(t for t in intelligence.analyze(lead)["tags"] if t["key"] == "whatsapp")["state"], "present")

    def test_demo_and_legacy_quality_not_false_verification(self):
        lead = {"emails": ["hola@negocio.test"]}
        result = intelligence.analyze(lead)
        self.assertEqual(result["data_quality"]["evidence_coverage"], 0)
        self.assertIn("sin validar", result["data_quality"]["email_status"])
        lead["provenance"] = intelligence.initial_provenance(lead, "demo", demo=True)
        self.assertTrue(intelligence.analyze(lead)["data_quality"]["demo"])

    def test_evidence_merge_refreshes_without_duplicates(self):
        old = intelligence.evidence("hola@negocio.test", "website", "https://negocio.test", observed_at="2026-01-01")
        new = intelligence.evidence("hola@negocio.test", "website", "https://negocio.test", observed_at="2026-10-06")
        merged = intelligence.merge_provenance({"emails": [old]}, {"emails": [new]})
        self.assertEqual(len(merged["emails"]), 1)
        self.assertEqual(merged["emails"][0]["observed_at"], "2026-10-06")


class ExtractionTests(unittest.TestCase):
    def test_jsonld_visible_phones_socials_and_query_whatsapp(self):
        html = '''<title>Negocio</title><meta name="viewport" content="width=device-width">
        <script>const tracking = "spam@tracking.test";</script>
        <script type="application/ld+json">{"@type":"Organization","email":"hola@negocio.test","contactPoint":{"telephone":"+57 300 123 4567"},"sameAs":["https://www.linkedin.com/company/negocio","https://instagram.com/negocio"]}</script>
        <p>Teléfono: 601 765 4321</p><a href="tel:+573111234567">Llamar</a>
        <a href="https://api.whatsapp.com/send?text=Hola&amp;phone=573001234567">WhatsApp</a>
        <form><input type="email" name="correo"><textarea name="mensaje"></textarea></form>'''
        data = website.extract(html, "https://negocio.test")
        self.assertIn("hola@negocio.test", data["emails"])
        self.assertNotIn("spam@tracking.test", data["emails"])
        self.assertIn("6017654321", data["phones"])
        self.assertIn("+573001234567", data["phones"])
        self.assertEqual(data["socials"]["linkedin"], "https://www.linkedin.com/company/negocio")
        self.assertEqual(data["wa"], ["573001234567"])
        self.assertTrue(data["audit"]["has_contact_form"])

    def test_malformed_obfuscation_and_social_share_links(self):
        data = website.extract('<a data-cfemail="bad" href="https://instagram.com/reel/abc">X</a><a href="https://linkedin.com.evil.test/company/acme">Y</a>', "https://negocio.test")
        self.assertNotIn("linkedin", data["socials"])
        self.assertNotIn("instagram", data["socials"])


class CrawlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.public_patch = patch.object(website, "public_url", AsyncMock(return_value=True))
        self.public_patch.start()
        self.addCleanup(self.public_patch.stop)

    async def test_contact_page_evidence_is_exact_and_robots_per_page(self):
        calls = []
        def handle(request):
            calls.append(request.url.path)
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text="User-agent: *\nDisallow: /contacto-privado")
            if request.url.path == "/":
                text = '<title>Negocio</title><a href="/contacto">Contacto</a><a href="/contacto-privado">Otro contacto</a>'
            else:
                text = '<a href="mailto:ventas@negocio.test">Correo</a><a href="https://www.linkedin.com/company/negocio">LinkedIn</a><a href="tel:+573001234567">Tel</a>'
            return httpx.Response(200, text=text, headers={"content-type": "text/html"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            data = await website.enrich(client, "https://negocio.test")
        self.assertEqual(data["emails"], ["ventas@negocio.test"])
        self.assertEqual(data["provenance"]["emails"][0]["url"], "https://negocio.test/contacto")
        self.assertTrue(data["web_audit"]["partial"])
        self.assertNotIn("/contacto-privado", calls)

    async def test_robots_denied_does_not_fetch_page(self):
        calls = []
        def handle(request):
            calls.append(request.url.path)
            return httpx.Response(200, text="User-agent: *\nDisallow: /")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            data = await website.enrich(client, "https://negocio.test")
        self.assertEqual(data["web_audit"]["status"], "blocked_robots")
        self.assertEqual(calls, ["/robots.txt"])

    async def test_redirect_checks_target_robots(self):
        calls = []
        def handle(request):
            calls.append(str(request.url))
            if request.url.host == "blocked.test":
                return httpx.Response(200, text="User-agent: *\nDisallow: /")
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(302, headers={"location": "https://blocked.test/contacto"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            data = await website.enrich(client, "https://negocio.test")
        self.assertEqual(data["web_audit"]["status"], "blocked_robots")
        self.assertNotIn("https://blocked.test/contacto", calls)

    async def test_forbidden_not_scored_as_broken_website(self):
        def handle(request):
            return httpx.Response(404) if request.url.path == "/robots.txt" else httpx.Response(403)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            data = await website.enrich(client, "https://negocio.test")
        self.assertEqual(data["web_audit"]["status"], "access_restricted")
        self.assertFalse(intelligence.analyze({"website": "https://negocio.test", **data})["opportunities"])

    async def test_http_500_is_opportunity_network_failure_is_not(self):
        def handle(request):
            return httpx.Response(404) if request.url.path == "/robots.txt" else httpx.Response(500)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            data = await website.enrich(client, "https://negocio.test")
        self.assertIn("web_error", [o["key"] for o in intelligence.analyze(data)["opportunities"]])

    async def test_linkedin_robots_and_authwall(self):
        def denied(request):
            return httpx.Response(200, text="User-agent: *\nDisallow: /")
        async with httpx.AsyncClient(transport=httpx.MockTransport(denied)) as client:
            data = await linkedin.lookup(client, "https://www.linkedin.com/company/negocio")
        self.assertEqual(data["linkedin_data"]["status"], "blocked_robots")
        def wall(request):
            return httpx.Response(404) if request.url.path == "/robots.txt" else httpx.Response(200, text="authwall Sign in")
        async with httpx.AsyncClient(transport=httpx.MockTransport(wall)) as client:
            data = await linkedin.lookup(client, "https://www.linkedin.com/company/negocio")
        self.assertEqual(data["linkedin_data"]["status"], "login_required")

    async def test_linkedin_public_company_extraction_and_personal_limit(self):
        calls = []
        def handle(request):
            calls.append(str(request.url))
            return httpx.Response(404) if request.url.path == "/robots.txt" else httpx.Response(200, text='<title>Negocio | LinkedIn</title><a href="mailto:hola@negocio.test">Email</a>')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            company = await linkedin.lookup(client, "https://www.linkedin.com/company/negocio")
            count = len(calls)
            person = await linkedin.lookup(client, "https://www.linkedin.com/in/persona")
        self.assertEqual(company["emails"], ["hola@negocio.test"])
        self.assertEqual(company["provenance"]["emails"][0]["source"], "linkedin_public")
        self.assertEqual(person["linkedin_data"]["status"], "personal_profile")
        self.assertEqual(len(calls), count)
