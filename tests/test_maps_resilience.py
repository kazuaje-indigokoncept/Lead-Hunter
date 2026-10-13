"""Chromium en servidores con poca memoria compartida: flags, mensaje real y reintento tras un cuelgue."""
import asyncio
import unittest

from app.sources import maps_web


class FakeLocator:
    def __init__(self, text="Resultados"):
        self.text = text

    async def inner_text(self, timeout=None):
        return self.text

    async def count(self):
        return 0

    async def wait_for(self, **_):
        if self.text == "crash":
            raise Exception("Locator.wait_for: Page crashed")

    @property
    def first(self):
        return self


class FakeResponse:
    status = 200


class FakePage:
    def __init__(self, fail=None, wait_crash=False):
        self.fail, self.url, self.closed, self.visits = fail, "https://www.google.com/maps/search/x", False, []
        self.wait_crash = wait_crash

    async def goto(self, url, **_):
        self.visits.append(url)
        if self.fail:
            raise Exception(self.fail)
        return FakeResponse()

    def get_by_role(self, *a, **k):
        return FakeLocator()

    def locator(self, *_):
        return FakeLocator("crash" if self.wait_crash else "Resultados")

    async def evaluate(self, *_):
        return {"name": "Café", "address": "Calle 1"}

    def set_default_timeout(self, *_):
        pass

    async def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, pages):
        self.pages = pages

    async def new_page(self):
        return self.pages.pop(0)


def browser(page, spare, detail=None):
    b = maps_web.MapsBrowser(log=lambda m: b.logs.append(m))
    b.logs = []
    b.page, b.detail, b.context = page, detail or FakePage(), FakeContext(spare)
    return b


class ResilienceTests(unittest.TestCase):
    def test_flags_avoid_small_shared_memory(self):
        self.assertIn("--disable-dev-shm-usage", maps_web.CHROMIUM_ARGS)

    def test_crashed_tab_is_replaced_and_retried_once(self):
        crashed, fresh = FakePage(fail="page.goto: Page crashed"), FakePage()
        b = browser(crashed, [fresh])
        asyncio.run(b._navigate(b.page, "https://www.google.com/maps/search/cafe"))
        self.assertTrue(crashed.closed)
        self.assertIs(b.page, fresh)
        self.assertEqual(fresh.visits, ["https://www.google.com/maps/search/cafe"])
        self.assertIn("Reintentando", b.logs[0])

    def test_second_crash_reports_memory_hint(self):
        b = browser(FakePage(fail="Page crashed"), [FakePage(fail="Target closed")])
        with self.assertRaises(maps_web.MapsWebError) as ctx:
            asyncio.run(b._navigate(b.page, "https://www.google.com/maps/search/cafe"))
        self.assertIn("Target closed", str(ctx.exception))
        self.assertIn("falta de memoria", str(ctx.exception))

    def test_network_errors_show_real_cause_without_retry(self):
        page = FakePage(fail="page.goto: net::ERR_NAME_NOT_RESOLVED at https://www.google.com/maps/search/cafe?hl=es\nCall log:\n - x")
        b = browser(page, [])
        with self.assertRaises(maps_web.MapsWebError) as ctx:
            asyncio.run(b._navigate(b.page, "https://www.google.com/maps/search/cafe"))
        self.assertEqual(str(ctx.exception), "No se pudo cargar Google Maps: net::ERR_NAME_NOT_RESOLVED.")
        self.assertEqual(len(page.visits), 1)

    def test_detail_crash_while_waiting_is_retried_and_campaign_continues(self):
        crashed, fresh = FakePage(wait_crash=True), FakePage()
        b = browser(FakePage(), [fresh], detail=crashed)
        raw = asyncio.run(b._read_detail("https://www.google.com/maps/place/cafe"))
        self.assertEqual(raw["name"], "Café")
        self.assertIs(b.detail, fresh)
        self.assertIn("Page crashed", b.logs[0])

    def test_detail_that_crashes_twice_is_skipped(self):
        b = browser(FakePage(), [FakePage(wait_crash=True), FakePage()], detail=FakePage(wait_crash=True))
        self.assertIsNone(asyncio.run(b._read_detail("https://www.google.com/maps/place/cafe")))
        self.assertIn("Ficha omitida", b.logs[-1])


if __name__ == "__main__":
    unittest.main()
