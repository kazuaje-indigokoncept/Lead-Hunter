"""Contactos públicos, redes, JSON-LD y auditoría de páginas del negocio."""
import json
import asyncio
import ipaddress
import re
import socket
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from ..intelligence import evidence, now

UA = "LeadHunter/2.0 (business contact research)"
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}")
CFEMAIL_RE = re.compile(r'''data-cfemail\s*=\s*["']([0-9a-fA-F]+)["']''')
CONTACT_HINTS = ("contact", "contacto", "contactenos", "contactanos", "nosotros", "about", "quienes-somos", "ubicacion", "sedes", "equipo", "team", "leadership", "direccion")
BAD_EMAIL_PARTS = ("example.", "sentry", "wixpress", "domain.com", "email.com", "yourdomain", "tudominio", "@2x", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", "godaddy", "u003e")
SOCIAL = {
    "instagram": re.compile(r"https?://(?:www\.)?instagram\.com/([A-Za-z0-9_.]{2,30})(?=[/?#\s]|$)", re.I),
    "facebook": re.compile(r"https?://(?:www\.|m\.|web\.)?facebook\.com/([A-Za-z0-9_.\-]{2,80}(?:/?\?id=\d+)?)(?=[/?#\s]|$)", re.I),
    "tiktok": re.compile(r"https?://(?:www\.)?tiktok\.com/@([A-Za-z0-9_.]{2,30})(?=[/?#\s]|$)", re.I),
    "linkedin": re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/(company|in)/([A-Za-z0-9_\-%]{2,100})(?=[/?#\s]|$)", re.I),
}
SOCIAL_SKIP = {"p", "reel", "reels", "explore", "stories", "sharer", "sharer.php", "share", "tr", "plugins", "dialog", "intent", "hashtag", "watch", "accounts", "profile.php", "groups", "events", "login", "privacy", "legal"}
SOCIAL_HOSTS = ("instagram.com", "facebook.com", "linkedin.com", "tiktok.com", "google.com", "wa.me", "whatsapp.com")


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hrefs, self.text, self.structured, self.forms = [], [], [], []
        self.hidden = 0
        self.script = None
        self.buffer = []
        self.in_title = False
        self.title = ""
        self.meta = {}
        self.form = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("script", "style", "noscript"):
            self.hidden += 1
            if tag == "script":
                self.script = attrs.get("type", "").lower() == "application/ld+json"
                self.buffer = []
        if tag == "title":
            self.in_title = True
        if tag == "a" and attrs.get("href"):
            self.hrefs.append(attrs["href"])
        if tag == "meta":
            self.meta[attrs.get("name", "").lower()] = attrs.get("content", "")
        if tag == "form":
            self.form = {"inputs": []}
            self.forms.append(self.form)
        if tag in ("input", "textarea") and self.form is not None:
            self.form["inputs"].append((attrs.get("type", tag), attrs.get("name", "")))

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            if tag == "script" and self.script:
                try:
                    self.structured.append(json.loads("".join(self.buffer)))
                except (ValueError, TypeError):
                    pass
            self.script = None
            self.hidden = max(0, self.hidden - 1)
        if tag == "title":
            self.in_title = False
        if tag == "form":
            self.form = None

    def handle_data(self, data):
        if self.script:
            self.buffer.append(data)
        if self.in_title:
            self.title += data
        if not self.hidden:
            self.text.append(data)


def _decode_cf(hexstr):
    try:
        key = int(hexstr[:2], 16)
        return "".join(chr(int(hexstr[i:i + 2], 16) ^ key) for i in range(2, len(hexstr), 2))
    except ValueError:
        return ""


def _clean_emails(found, site_host):
    emails = {e.strip(".").lower() for e in found if isinstance(e, str)}
    emails = [e for e in emails if EMAIL_RE.fullmatch(e) and len(e) <= 100 and not any(b in e for b in BAD_EMAIL_PARTS)]
    host = site_host.removeprefix("www.")
    return sorted(emails, key=lambda e: (not e.endswith("@" + host), e))


def clean_phone(value):
    value = str(value).strip()
    digits = re.sub(r"\D", "", value)
    return ("+" if value.startswith("+") else "") + digits if 8 <= len(digits) <= 15 else None


def _json_contacts(node, include_people=True):
    emails, phones, links = [], [], []
    def walk(n):
        if isinstance(n, list):
            for item in n:
                walk(item)
        elif isinstance(n, dict):
            types = n.get("@type", [])
            if not include_people and (types == "Person" or isinstance(types, list) and "Person" in types):
                return
            for key, val in n.items():
                values = val if isinstance(val, list) else [val]
                if key == "email":
                    emails.extend(v.removeprefix("mailto:") for v in values if isinstance(v, str))
                if key == "telephone":
                    phones.extend(v for v in values if isinstance(v, str))
                if key in ("sameAs", "url"):
                    links.extend(v for v in values if isinstance(v, str))
                if isinstance(val, (dict, list)):
                    walk(val)
    walk(node)
    return emails, phones, links


def extract(html, base_url):
    parser = PageParser()
    parser.feed(html)
    hrefs = [urljoin(base_url, unescape(h.strip())) for h in parser.hrefs]
    text = " ".join(parser.text)
    emails = EMAIL_RE.findall(text) + [_decode_cf(h) for h in CFEMAIL_RE.findall(html)]
    emails += [unquote(h[7:].split("?")[0]) for h in hrefs if h.lower().startswith("mailto:")]
    phones = [unquote(h[4:].split("?")[0]) for h in hrefs if h.lower().startswith("tel:")]
    phones += re.findall(r"(?:tel[eé]fono|tel\.?|phone|celular|m[oó]vil|whatsapp)\s*[:\-]?\s*(\+?\d[\d\s().\-]{7,24}\d)", text, re.I)
    for data in parser.structured:
        em, ph, links = _json_contacts(data, include_people=False)
        emails += em
        phones += ph
        hrefs += links
    socials, wa = {}, []
    for h in hrefs:
        for net, rx in SOCIAL.items():
            m = rx.match(h)
            if m and m.group(m.lastindex).split("/")[0].lower() not in SOCIAL_SKIP:
                if net == "linkedin" and m.group(1) != "company":
                    continue
                socials.setdefault(net, m.group(0).rstrip("/"))
        parsed = urlparse(h)
        host = (parsed.hostname or "").lower()
        value = None
        if host in ("wa.me", "www.wa.me"):
            value = parsed.path.strip("/")
        elif host in ("api.whatsapp.com", "web.whatsapp.com", "www.whatsapp.com", "whatsapp.com") and parsed.path == "/send":
            value = (parse_qs(parsed.query).get("phone") or [None])[0]
        phone = clean_phone(value) if value else None
        if phone:
            wa.append(phone.lstrip("+"))
    phones = list(dict.fromkeys(p for p in (clean_phone(p) for p in phones) if p))
    form = any(any(t in ("email", "textarea") or re.search(r"email|correo|mensaje|message", name, re.I)
                   for t, name in f["inputs"]) for f in parser.forms)
    return {"emails": emails, "phones": phones, "wa": wa, "socials": socials, "hrefs": hrefs,
            "audit": {"has_contact_form": form, "has_viewport": bool(parser.meta.get("viewport")),
                      "has_meta_description": bool(parser.meta.get("description")), "has_title": bool(parser.title.strip())}}


async def _allowed(client, url):
    p = urlparse(url)
    try:
        r = await client.get(f"{p.scheme}://{p.netloc}/robots.txt", timeout=8, follow_redirects=False)
        if r.status_code in (404, 410):
            return True
        if r.status_code != 200:
            return False
        rp = RobotFileParser()
        rp.parse(r.text.splitlines())
        return rp.can_fetch(UA, url)
    except httpx.HTTPError:
        return False


async def public_url(url):
    """El rastreador visita webs públicas, no servicios locales o redes privadas."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username:
        return False
    try:
        addresses = await asyncio.get_running_loop().getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        return bool(addresses) and all(ipaddress.ip_address(addr[4][0]).is_global for addr in addresses)
    except (socket.gaierror, ValueError, OSError):
        return False


async def fetch_page(client, url):
    """Comprueba robots en cada salto antes de seguir redirecciones."""
    for _ in range(6):
        parsed = urlparse(url)
        if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username:
            return None, url, "invalid_url"
        if not await public_url(url):
            return None, url, "non_public_url"
        if not await _allowed(client, url):
            return None, url, "blocked_robots"
        r = await client.get(url, timeout=15, follow_redirects=False)
        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
            url = urljoin(str(r.url), r.headers["location"])
            continue
        return r, str(r.url), None
    return None, url, "redirect_limit"


async def enrich(client, url, max_extra_pages=4):
    if not url:
        return {}
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    host = (urlparse(url).hostname or "").lower()
    audit = {"status": "not_checked", "checked_at": now(), "resolved_url": url, "pages": [], "partial": False}
    if any(host == h or host.endswith("." + h) for h in SOCIAL_HOSTS):
        audit["status"] = "social_url"
        return {"web_audit": audit, "error": "La dirección es una red social, no una web del negocio."}
    pages = []
    people_found = []
    try:
        r, final, error = await fetch_page(client, url)
        audit["resolved_url"] = final
        if error:
            audit["status"] = error
            return {"web_audit": audit, "error": f"No se pudo revisar la web ({error})."}
        audit["status_code"] = r.status_code
        if r.status_code in (401, 403, 429):
            audit["status"] = "access_restricted"
            return {"web_audit": audit, "error": f"Acceso restringido (HTTP {r.status_code}); no confirma fallo de la web."}
        if r.status_code >= 400:
            audit["status"] = "http_error"
            return {"web_audit": audit, "error": f"Web respondió HTTP {r.status_code}"}
        if "html" not in r.headers.get("content-type", "").lower():
            audit["status"] = "not_html"
            return {"web_audit": audit, "error": "La web no devolvió HTML."}
        first = extract(r.text, final)
        from . import people
        people_found.extend(people.extract(r.text, final))
        pages.append((final, first))
        audit.update(first["audit"])
        audit.update(status="ok", https=urlparse(final).scheme == "https")
        candidates = []
        for h in first["hrefs"]:
            ph = urlparse(h)
            if ph.scheme in ("http", "https") and ph.hostname == urlparse(final).hostname and any(k in ph.path.lower() for k in CONTACT_HINTS):
                h = h.split("#")[0]
                if h != final and h not in candidates:
                    candidates.append(h)
        for h in candidates[:max_extra_pages]:
            try:
                rr, target, err = await fetch_page(client, h)
                if err or rr is None or rr.status_code != 200 or urlparse(target).hostname != urlparse(final).hostname or "html" not in rr.headers.get("content-type", ""):
                    audit["partial"] = True
                    continue
                ex = extract(rr.text, target)
                people_found.extend(people.extract(rr.text, target))
                pages.append((target, ex))
                audit["has_contact_form"] |= ex["audit"]["has_contact_form"]
            except httpx.HTTPError:
                audit["partial"] = True
    except httpx.HTTPError as exc:
        audit["status"] = "network_error"
        return {"web_audit": audit, "error": f"Web no accesible ({type(exc).__name__}); no confirma que esté caída."}
    audit["pages"] = [base for base, _ in pages]
    all_emails = _clean_emails([e for _, ex in pages for e in ex["emails"]], urlparse(audit["resolved_url"]).hostname or "")[:10]
    phones = list(dict.fromkeys(p for _, ex in pages for p in ex["phones"]))[:10]
    out = {"emails": all_emails, "phones": phones, "web_audit": audit, "provenance": {}, "people_candidates": people_found[:60]}
    for base, ex in pages:
        contacts = {"emails": [e for e in _clean_emails(ex["emails"], host) if e in all_emails],
                    "phones": [p for p in ex["phones"] if p in phones], **ex["socials"]}
        if ex["wa"]:
            contacts["whatsapp"] = "https://wa.me/" + ex["wa"][0]
        for field, values in contacts.items():
            values = values if isinstance(values, list) else [values]
            for value in values:
                out["provenance"].setdefault(field, []).append(evidence(value, "website", base, confidence="media"))
                if field not in ("emails", "phones"):
                    out.setdefault(field, value)
    if phones:
        out["phone_web"] = phones[0]
        out["provenance"]["phone"] = [r for r in out["provenance"].get("phones", []) if r["value"] == phones[0]]
    return out


def ig_username(url):
    m = SOCIAL["instagram"].match(url or "")
    return m.group(1) if m else None
