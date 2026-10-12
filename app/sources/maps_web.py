"""Google Maps público mediante Chromium, sin API ni endpoints privados."""
import asyncio
import hashlib
import re
from urllib.parse import parse_qs, quote, unquote, urlparse

from .places import to_wa

# Un navegador a la vez para limitar memoria y tráfico de campañas concurrentes.
_browser_lock = asyncio.Lock()


class MapsWebError(Exception):
    pass


class MapsBlockedError(MapsWebError):
    pass


def normalize(raw: dict) -> dict:
    url = raw.get("maps_url", "")
    identity = re.search(r"!1s([^!/?]+)", unquote(url))
    key = identity.group(1) if identity else hashlib.sha256(
        f"{raw.get('name', '')}|{raw.get('address', '')}".casefold().encode()
    ).hexdigest()[:24]
    phone = raw.get("phone") or None
    # Un número nacional por sí solo no demuestra código de país ni WhatsApp.
    wa = to_wa(phone) if phone and phone.startswith("+") else None
    rating = re.search(r"[0-5](?:[.,]\d+)?", raw.get("rating") or "")
    reviews = re.search(r"[\d][\d.,\s]*", raw.get("reviews") or "")
    web = raw.get("website") or None
    if web:
        parsed = urlparse(web)
        if parsed.hostname in ("google.com", "www.google.com") and parsed.path == "/url":
            web = (parse_qs(parsed.query).get("q") or parse_qs(parsed.query).get("url") or [None])[0]
        if web and urlparse(web).scheme not in ("http", "https"):
            web = None
    text = (raw.get("status") or "").casefold()
    closed = any(s in text for s in ("cerrado permanentemente", "permanently closed"))
    return {
        "dedupe_key": "gmaps:" + key, "source": "google_maps",
        "name": raw.get("name"), "category": raw.get("category") or None,
        "address": raw.get("address") or None, "phone": phone,
        "whatsapp": wa, "website": web, "maps_url": url,
        "rating": float(rating.group().replace(",", ".")) if rating else None,
        "reviews": int(re.sub(r"\D", "", reviews.group())) if reviews else None,
        "business_status": "CLOSED_PERMANENTLY" if closed else None,
    }


# Se leen elementos visibles de la ficha. No se consultan APIs internas.
DETAIL_JS = """() => {
    const text = s => document.querySelector(s)?.innerText?.trim() || '';
    const label = s => document.querySelector(s)?.getAttribute('aria-label') || '';
    const strip = s => s.replace(/^(Dirección|Address|Teléfono|Phone):\\s*/i, '');
    const rating = document.querySelector('[role="img"][aria-label*="estrellas"], [role="img"][aria-label*="stars"]');
    const reviews = document.querySelector('button[jsaction*="reviewChart"]');
    return {
        name: text('h1'),
        category: text('button[jsaction*="category"]'),
        address: strip(label('[data-item-id="address"]')) || text('[data-item-id="address"] .Io6YTe'),
        phone: strip(label('[data-item-id^="phone:tel:"]')) || text('[data-item-id^="phone:tel:"] .Io6YTe'),
        website: document.querySelector('a[data-item-id="authority"]')?.href || '',
        rating: rating?.getAttribute('aria-label') || text('.F7nice span[aria-hidden="true"]'),
        reviews: reviews?.getAttribute('aria-label') || reviews?.innerText ||
            label('.F7nice span[aria-label*="reseñas"], .F7nice span[aria-label*="reviews"]'),
        status: text('[role="main"]')
    };
}"""


class MapsBrowser:
    def __init__(self, log=None, check_stop=None):
        self.log = log or (lambda msg: None)
        self.check_stop = check_stop or (lambda: None)
        self.driver = self.browser = self.context = None

    async def __aenter__(self):
        await _browser_lock.acquire()
        try:
            self.check_stop()
            from playwright.async_api import async_playwright
            self.driver = await async_playwright().start()
            self.browser = await self.driver.chromium.launch(headless=True)
            self.context = await self.browser.new_context(locale="es-CO", viewport={"width": 1280, "height": 900})
            self.page = await self.context.new_page()
            self.detail = await self.context.new_page()
            self.page.set_default_timeout(10000)
            self.detail.set_default_timeout(10000)
            return self
        except BaseException as exc:
            await self.__aexit__(None, None, None)
            if isinstance(exc, ImportError):
                raise MapsWebError("Instala dependencias: python -m pip install -r requirements.txt") from exc
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise MapsWebError(
                "No se pudo iniciar Chromium. Ejecuta: python -m playwright install chromium. "
                "En Linux: python -m playwright install --with-deps chromium. " + str(exc)[:180]
            ) from exc

    async def __aexit__(self, *_):
        try:
            if self.browser:
                await self.browser.close()
        finally:
            try:
                if self.driver:
                    await self.driver.stop()
            finally:
                _browser_lock.release()

    async def _check_block(self, page):
        text = (await page.locator("body").inner_text(timeout=5000)).casefold()
        if "/sorry/" in page.url or any(s in text for s in (
            "unusual traffic", "tráfico inusual", "trafico inusual", "not a robot", "no soy un robot"
        )) or await page.locator('iframe[src*="recaptcha"], #captcha-form').count():
            raise MapsBlockedError("Google Maps mostró un captcha o bloqueo. Se detuvo la búsqueda; los leads obtenidos se conservan.")

    async def _navigate(self, page, url):
        self.check_stop()
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            if response and response.status in (403, 429):
                raise MapsBlockedError(f"Google Maps bloqueó la búsqueda (HTTP {response.status}). Los leads obtenidos se conservan.")
            if response and response.status >= 400:
                raise MapsWebError(f"Google Maps respondió HTTP {response.status}.")
            # Rechazar cookies opcionales cuando Google muestra su diálogo.
            reject = page.get_by_role("button", name=re.compile(r"^(Rechazar todo|Rechazar todas|Reject all)$", re.I))
            if await reject.count():
                await reject.first.click()
            await self._check_block(page)
            self.check_stop()
        except MapsWebError:
            raise
        except Exception as exc:
            raise MapsWebError(f"No se pudo cargar Google Maps: {type(exc).__name__}") from exc

    async def search(self, query, *, max_results=40, min_rating=None, delay=2):
        """Genera fichas progresivamente para conservar resultados si hay un bloqueo."""
        from playwright.async_api import TimeoutError as PlaywrightTimeout
        url = f"https://www.google.com/maps/search/{quote(query, safe='')}?hl=es"
        await self._navigate(self.page, url)
        try:
            await self.page.locator('[role="feed"], h1.DUwDvf').first.wait_for(state="visible", timeout=20000)
        except PlaywrightTimeout:
            await self._check_block(self.page)
            text = (await self.page.locator("body").inner_text()).casefold()
            if any(s in text for s in ("no se han encontrado", "no encontramos", "no results", "no se encontraron")):
                return
            raise MapsWebError("No se reconoció el panel de resultados de Google Maps. Puede haber cambiado su diseño o no haber cargado.")
        feed = self.page.locator('[role="feed"]').first
        if not await feed.count():
            raw = await self.page.evaluate(DETAIL_JS)
            raw["maps_url"] = self.page.url
            lead = normalize(raw)
            if lead["name"] and (min_rating is None or (lead["rating"] or 0) >= min_rating):
                yield lead
            return
        seen, stale = set(), 0
        while len(seen) < max_results and stale < 4:
            self.check_stop()
            await self._check_block(self.page)
            links = await feed.locator('a[href*="/maps/place/"]').evaluate_all("els => els.map(el => el.href)")
            fresh = [link for link in dict.fromkeys(links) if link not in seen]
            stale = 0 if fresh else stale + 1
            for link in fresh:
                if len(seen) >= max_results:
                    break
                seen.add(link)
                await asyncio.sleep(delay)
                await self._navigate(self.detail, link)
                try:
                    await self.detail.locator('h1.DUwDvf').wait_for(state="visible", timeout=15000)
                    # El encabezado puede aparecer antes de los datos de contacto.
                    await self.detail.locator('[data-item-id="address"], button[jsaction*="category"]').first.wait_for(timeout=10000)
                except PlaywrightTimeout:
                    await self._check_block(self.detail)
                    self.log("Ficha omitida: no terminó de cargar.")
                    continue
                raw = await self.detail.evaluate(DETAIL_JS)
                raw["maps_url"] = self.detail.url
                lead = normalize(raw)
                if lead["name"] and (min_rating is None or (lead["rating"] or 0) >= min_rating):
                    yield lead
            await feed.evaluate("el => { el.scrollTop = el.scrollHeight; }")
            await asyncio.sleep(delay)
        if not seen:
            raise MapsWebError("El panel de Maps no contiene fichas reconocibles. Revisa la búsqueda o el diseño de la página.")
