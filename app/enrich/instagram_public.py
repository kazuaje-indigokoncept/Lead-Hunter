"""Lectura de HTML público de perfiles, sin sesión ni endpoints privados."""
import re
from urllib.parse import urlparse

import httpx
from . import website
from ..intelligence import evidence, now

RESERVED = website.SOCIAL_SKIP | {"direct", "challenge", "about", "developer", "developers", "api", "oauth", "static"}


def profile_url(value):
    p = urlparse(value or "")
    host = (p.hostname or "").lower()
    user = p.path.strip("/")
    if (p.scheme != "https" or host not in ("instagram.com", "www.instagram.com")
            or p.username or p.port not in (None, 443) or not re.fullmatch(r"[A-Za-z0-9_.]{1,30}", user)
            or user.lower() in RESERVED):
        return None
    return f"https://www.instagram.com/{user}/"


class ProfileParser(website.PageParser):
    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        attrs = dict(attrs)
        if tag == "meta" and attrs.get("property"):
            self.meta[attrs["property"].lower()] = attrs.get("content", "")


async def lookup(client, url):
    audit = {"status": "not_checked", "checked_at": now(), "url": url, "mode": "web"}
    def result(status, **extra):
        return {"instagram_data": {**audit, "status": status, **extra}}
    try:
        canonical = profile_url(url)
        if not canonical:
            return result("invalid_url")
        user = urlparse(canonical).path.strip("/")
        response, final, error = await website.fetch_page(client, canonical)
        audit["url"] = final
        if error:
            return result(error)
        audit["status_code"] = response.status_code
        if response.status_code in (401, 403, 429):
            return result("access_restricted")
        if response.status_code == 404:
            return result("not_found")
        if response.status_code != 200:
            return result("http_error")
        if profile_url(final) != canonical:
            return result("login_required")
        if "html" not in response.headers.get("content-type", "").lower():
            return result("not_html")
        parser = ProfileParser()
        parser.feed(response.text)
        visible = " ".join(parser.text)
        if re.search(r"captcha|confirm you're human|security check|confirma que eres humano", visible, re.I):
            return result("login_required")
        title = parser.meta.get("og:title", parser.title).strip()
        description = parser.meta.get("og:description", parser.meta.get("description", "")).strip()
        # Un HTML genérico de acceso o una carcasa JS no demuestra revisión del perfil.
        if not re.search(r"@" + re.escape(user) + r"(?=[\s)·]|$)", title, re.I) or not description:
            return result("unavailable_content", note="El HTML no publica datos suficientes del perfil; puede requerir sesión o JavaScript.")
        bio_match = re.search(r"(?:on Instagram|en Instagram)\s*:\s*[\"“](.*)[\"”]\s*$", description, re.I | re.S)
        bio = bio_match.group(1).strip() if bio_match else None
        # Solo números completos: no se transforma un contador abreviado en una cifra exacta.
        count = re.search(r"(?<![\w.,])([0-9]+(?:,[0-9]{3})*)\s+Followers\b", description, re.I)
        followers = int(count.group(1).replace(",", "")) if count else None
        # Metadatos públicos y texto visible; nunca se analizan scripts internos de Instagram.
        contacts = website.extract(response.text, final)
        contacts["emails"] += website.EMAIL_RE.findall(bio or description)
        bio_contacts = website.extract((bio or description).replace("<", "&lt;").replace(">", "&gt;"), final)
        emails = website._clean_emails(contacts["emails"], "")[:10]
        bio_numbers = re.findall(r"(?<!\w)\+\d[\d ().-]{8,23}\d", bio or "")
        bio_numbers = [p for p in (website.clean_phone(v) for v in bio_numbers) if p and len(p.lstrip("+")) >= 10]
        phones = list(dict.fromkeys(contacts["phones"] + bio_contacts["phones"] + bio_numbers))[:10]
        links = contacts["hrefs"] + re.findall(r"https?://[^\s<>\"“”]+", bio or "")
        links = list(dict.fromkeys(u.rstrip(".,)") for u in links))
        external = [u for u in links if urlparse(u).scheme in ("http", "https") and urlparse(u).hostname
                    and not any((urlparse(u).hostname or "").lower() == h or (urlparse(u).hostname or "").lower().endswith("." + h)
                                for h in ("instagram.com", "facebook.com", "meta.com"))]
        out = result("public_page", title=title, description=description, external_links=external[:10], partial=True,
                     note="Lectura parcial del HTML público; contactos publicados, sin validar entrega ni identidad.")
        out.update(emails=emails, phones=phones, provenance={})
        for field, values in (("emails", emails), ("phones", phones), ("ig_bio", [bio]), ("ig_followers", [followers])):
            values = [v for v in values if v is not None]
            if values:
                out["provenance"][field] = [evidence(v, "instagram_public", final) for v in values]
        if bio is not None:
            out["ig_bio"] = bio
        if followers is not None:
            out["ig_followers"] = followers
        if phones:
            out["phone_web"] = phones[0]
            out["provenance"]["phone"] = [evidence(phones[0], "instagram_public", final)]
        # Enlaces de WhatsApp explícitos en la biografía o en la página.
        link_html = "".join('<a href="' + u.replace('&', '&amp;').replace('"', '&quot;') + '"></a>' for u in links)
        wa = website.extract(link_html, final)["wa"]
        if wa:
            out["whatsapp"] = "https://wa.me/" + wa[0]
            out["provenance"]["whatsapp"] = [evidence(out["whatsapp"], "instagram_public", final)]
        return out
    except (ValueError, httpx.InvalidURL):
        return result("invalid_url")
    except httpx.HTTPError:
        return result("network_error")
