"""Google Maps público mediante Chromium, sin API ni endpoints privados."""
import asyncio
import os
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


# En Docker (Railway, etc.) /dev/shm suele tener 64 MB y Chromium se cuelga con Google Maps.
# --disable-dev-shm-usage hace que use /tmp; el resto reduce memoria en un servidor sin pantalla.
CHROMIUM_ARGS = ["--disable-dev-shm-usage", "--disable-gpu", "--disable-extensions", "--mute-audio",
                 "--no-first-run", "--disable-background-networking"]
# Imágenes, vídeo y fuentes no hacen falta para leer las fichas y son lo que más memoria ocupa.
# MAPS_BLOCK_MEDIA=0 en las variables del servidor lo desactiva si alguna vez afectara la lectura.
BLOCKED_TYPES = {"image", "media", "font"}
CRASH_SIGNS = ("crash", "target closed", "has been closed", "browser has disconnected", "out of memory")


def describe(exc) -> str:
    """Primera línea útil del error de Playwright (no solo su tipo)."""
    msg = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
    msg = re.sub(r"^\w+\.\w+: ", "", msg)            # "page.goto: net::ERR_..." → "net::ERR_..."
    msg = re.sub(r"\s+at https?://\S+$", "", msg)   # sin la URL completa de la búsqueda
    return msg[:180]


def is_crash(exc) -> bool:
    return any(sign in str(exc).lower() for sign in CRASH_SIGNS)


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
            self.browser = await self.driver.chromium.launch(headless=True, args=CHROMIUM_ARGS)
            self.context = await self.browser.new_context(locale="es-CO", viewport={"width": 1280, "height": 900})
            if os.getenv("MAPS_BLOCK_MEDIA", "1") != "0":
                await self.context.route("**/*", lambda route: route.abort() if route.request.resource_type in BLOCKED_TYPES
                                         else route.continue_())
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

    async def _replace(self, page):
        """Abre una pestaña nueva en lugar de una que se colgó."""
        try:
            await page.close()
        except Exception:  # noqa: BLE001 - ya estaba cerrada
            pass
        fresh = await self.context.new_page()
        fresh.set_default_timeout(10000)
        if page is self.page:
            self.page = fresh
        else:
            self.detail = fresh
        return fresh

    async def _navigate(self, page, url, retry=True):
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
            if retry and is_crash(exc):
                self.log(f"Chromium cerró la pestaña de Google Maps ({describe(exc)}). Reintentando una vez…")
                try:
                    fresh = await self._replace(page)
                except Exception as again:  # noqa: BLE001 - el navegador completo cayó
                    raise MapsWebError(f"Chromium se cerró al cargar Google Maps: {describe(again)}. "
                                       "Suele indicar falta de memoria en el servidor.") from again
                return await self._navigate(fresh, url, retry=False)
            hint = " Suele indicar falta de memoria en el servidor." if is_crash(exc) else ""
            raise MapsWebError(f"No se pudo cargar Google Maps: {describe(exc)}.{hint}") from exc

    async def _read_detail(self, link):
        """Lee una ficha. Si la pestaña se cuelga, la reabre y reintenta una vez; si vuelve a fallar, la omite."""
        from playwright.async_api import TimeoutError as PlaywrightTimeout
        for attempt in (1, 2):
            try:
                await self._navigate(self.detail, link, retry=False)
                await self.detail.locator('h1.DUwDvf').wait_for(state="visible", timeout=15000)
                # El encabezado puede aparecer antes de los datos de contacto.
                await self.detail.locator('[data-item-id="address"], button[jsaction*="category"]').first.wait_for(timeout=10000)
                raw = await self.detail.evaluate(DETAIL_JS)
                raw["maps_url"] = self.detail.url
                return raw
            except PlaywrightTimeout:
                await self._check_block(self.detail)
                self.log("Ficha omitida: no terminó de cargar.")
                return None
            except MapsBlockedError:
                raise
            except Exception as exc:
                cause = exc.__cause__ or exc
                if not is_crash(cause) and not is_crash(exc):
                    raise
                if attempt == 2:
                    self.log(f"Ficha omitida: Chromium volvió a cerrarse ({describe(cause)}).")
                    await self._replace(self.detail)
                    return None
                self.log(f"Chromium cerró la ficha ({describe(cause)}). Reintentando…")
                await self._replace(self.detail)
        return None

    async def search(self, query, *, max_results=40, min_rating=None, delay=2):
        """Genera fichas progresivamente para conservar resultados si hay un bloqueo."""
        from playwright.async_api import TimeoutError as PlaywrightTimeout
        url = f"https://www.google.com/maps/search/{quote(query, safe='')}?hl=es"
        await self._navigate(self.page, url)
        restarts = 0
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
            try:
                await self._check_block(self.page)
                links = await feed.locator('a[href*="/maps/place/"]').evaluate_all("els => els.map(el => el.href)")
            except MapsWebError:
                raise
            except Exception as exc:
                if not is_crash(exc) or restarts >= 2:
                    raise MapsWebError(f"Google Maps dejó de responder: {describe(exc)}.") from exc
                # La lista de resultados se colgó: se vuelve a abrir y se siguen omitiendo las fichas ya vistas.
                restarts += 1
                self.log(f"Chromium cerró la lista de resultados ({describe(exc)}). Reabriendo la búsqueda…")
                await self._navigate(await self._replace(self.page), url)
                await self.page.locator('[role="feed"]').first.wait_for(state="visible", timeout=20000)
                feed = self.page.locator('[role="feed"]').first
                continue
            fresh = [link for link in dict.fromkeys(links) if link not in seen]
            stale = 0 if fresh else stale + 1
            for link in fresh:
                if len(seen) >= max_results:
                    break
                seen.add(link)
                await asyncio.sleep(delay)
                raw = await self._read_detail(link)
                if raw is None:
                    continue
                lead = normalize(raw)
                if lead["name"] and (min_rating is None or (lead["rating"] or 0) >= min_rating):
                    yield lead
            await feed.evaluate("el => { el.scrollTop = el.scrollHeight; }")
            await asyncio.sleep(delay)
        if not seen:
            raise MapsWebError("El panel de Maps no contiene fichas reconocibles. Revisa la búsqueda o el diseño de la página.")
