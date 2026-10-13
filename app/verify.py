"""Verificación gratuita de correos y teléfonos, sin enviar mensajes ni llamar.

Correos: sintaxis, nombres de archivo capturados por error (logo@2x.png), dominios
desechables o mal escritos y si el dominio tiene servidor de correo (DNS MX).
Comprobar que un buzón concreto existe exige un servicio de pago; aquí no se hace.

Teléfonos: con la librería de Google (phonenumbers) se comprueba que el número es
válido para su país y si es móvil o fijo. No se comprueba si la línea está activa.
"""
import asyncio
import re
import time

import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberType

from . import db, intelligence

DNS_TIMEOUT = 4.0
DNS_CACHE_TTL = 6 * 3600
RECHECK_DAYS = 30
_dns_cache: dict[str, tuple[float, str]] = {}

EMAIL_SYNTAX = re.compile(r"^[A-Za-z0-9._%+\-]{1,64}@([A-Za-z0-9\-]+\.)+[A-Za-z]{2,24}$")
FILE_TLDS = {"png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "ico", "css", "js", "pdf", "mp4", "avif", "php", "html"}
PLACEHOLDER_DOMAINS = {"example.com", "example.org", "domain.com", "email.com", "correo.com", "tudominio.com",
                       "yourdomain.com", "dominio.com", "test.com", "sentry.io", "sentry.wixpress.com",
                       "sentry-next.wixpress.com", "wixpress.com", "godaddy.com"}
PLACEHOLDER_LOCALS = {"tucorreo", "tuemail", "youremail", "your-email", "nombre", "name", "email", "correo", "usuario", "user"}
DISPOSABLE = {
    "mailinator.com", "yopmail.com", "10minutemail.com", "guerrillamail.com", "guerrillamail.net", "sharklasers.com",
    "tempmail.com", "temp-mail.org", "tempmail.net", "throwawaymail.com", "trashmail.com", "getnada.com",
    "dispostable.com", "maildrop.cc", "fakeinbox.com", "mintemail.com", "mohmal.com", "emailondeck.com",
    "spamgourmet.com", "mailnesia.com", "mytemp.email", "tempail.com", "moakt.com", "inboxkitten.com",
    "burnermail.io", "33mail.com", "spambox.us", "mailcatch.com", "tempr.email", "discard.email",
}
TYPOS = {"gmial.com": "gmail.com", "gmal.com": "gmail.com", "gmai.com": "gmail.com", "gmail.co": "gmail.com",
         "gmail.con": "gmail.com", "gmail.cm": "gmail.com", "hotmial.com": "hotmail.com", "hotmai.com": "hotmail.com",
         "hotmail.con": "hotmail.com", "hotmail.co": "hotmail.com", "hotmal.com": "hotmail.com", "yahoo.con": "yahoo.com",
         "yaho.com": "yahoo.com", "outlok.com": "outlook.com", "outlook.con": "outlook.com", "gnail.com": "gmail.com"}

EMAIL_LABELS = {
    "valid_domain": "Dominio con servidor de correo",
    "no_mail_server": "El dominio no recibe correo",
    "domain_not_found": "El dominio no existe",
    "disposable": "Correo temporal o desechable",
    "typo": "Posible error de escritura",
    "placeholder": "Correo de ejemplo o de un servicio externo",
    "invalid": "Formato no válido",
    "unknown": "No se pudo comprobar ahora",
}
PHONE_LABELS = {"valid": "Número válido", "invalid": "Número no válido para su país", "unparseable": "No parece un teléfono"}
PHONE_TYPES = {PhoneNumberType.MOBILE: "móvil", PhoneNumberType.FIXED_LINE: "fijo",
               PhoneNumberType.FIXED_LINE_OR_MOBILE: "fijo o móvil", PhoneNumberType.TOLL_FREE: "línea gratuita",
               PhoneNumberType.VOIP: "VoIP", PhoneNumberType.PREMIUM_RATE: "tarifa especial",
               PhoneNumberType.SHARED_COST: "costo compartido", PhoneNumberType.UAN: "número corporativo"}
GOOD_EMAIL, GOOD_PHONE = {"valid_domain"}, {"valid"}


def check_email_static(email: str):
    """Comprobaciones sin red. Devuelve un estado definitivo o None si hay que consultar DNS."""
    email = (email or "").strip().lower()
    if not EMAIL_SYNTAX.match(email):
        return "invalid", None
    local, domain = email.rsplit("@", 1)
    if domain.rsplit(".", 1)[-1] in FILE_TLDS or re.search(r"@\d+x\.", email):
        return "invalid", None
    if domain in DISPOSABLE:
        return "disposable", None
    if domain in TYPOS:
        return "typo", TYPOS[domain]
    if domain in PLACEHOLDER_DOMAINS or domain.endswith(".wixpress.com") or local in PLACEHOLDER_LOCALS:
        return "placeholder", None
    return None, None


async def _dns_status(domain: str) -> str:
    cached = _dns_cache.get(domain)
    if cached and time.time() - cached[0] < DNS_CACHE_TTL:
        return cached[1]
    import dns.asyncresolver
    import dns.exception
    import dns.resolver
    resolver = dns.asyncresolver.Resolver()
    resolver.lifetime = DNS_TIMEOUT
    try:
        answer = await resolver.resolve(domain, "MX")
        status = "valid_domain" if any(str(r.exchange) not in (".", "") for r in answer) else "no_mail_server"
    except dns.resolver.NXDOMAIN:
        status = "domain_not_found"
    except dns.resolver.NoAnswer:
        # Sin MX, el correo se entrega al registro A del dominio (RFC 5321).
        try:
            await resolver.resolve(domain, "A")
            status = "valid_domain"
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
            status = "no_mail_server"
        except dns.exception.DNSException:
            status = "unknown"
    except dns.exception.DNSException:
        status = "unknown"
    if status != "unknown":
        _dns_cache[domain] = (time.time(), status)
    return status


async def check_email(email: str) -> dict:
    email = (email or "").strip().lower()
    status, suggestion = check_email_static(email)
    if status is None:
        status = await _dns_status(email.rsplit("@", 1)[1])
    out = {"status": status, "label": EMAIL_LABELS[status], "checked_at": intelligence.now()}
    if suggestion:
        out["suggestion"] = email.rsplit("@", 1)[0] + "@" + suggestion
    return out


def default_region(lead=None) -> str:
    region = ""
    if lead and lead.get("campaign_id"):
        camp = db.one("SELECT options FROM campaigns WHERE id = ?", (lead["campaign_id"],))
        region = ((camp or {}).get("options") or {}).get("region", "")
    return (region or db.get_setting("default_region", "CO") or "CO").upper()[:2]


def check_phone(phone: str, region: str = "CO") -> dict:
    out = {"checked_at": intelligence.now()}
    try:
        num = phonenumbers.parse(str(phone), region)
    except NumberParseException:
        return {**out, "status": "unparseable", "label": PHONE_LABELS["unparseable"]}
    if not phonenumbers.is_valid_number(num):
        return {**out, "status": "invalid", "label": PHONE_LABELS["invalid"]}
    kind = PHONE_TYPES.get(phonenumbers.number_type(num), "otro tipo")
    country = phonenumbers.region_code_for_number(num) or region
    return {**out, "status": "valid", "type": kind, "country": country,
            "e164": phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164),
            "label": f"{PHONE_LABELS['valid']} · {kind} · {country}"}


def lead_phones(lead):
    return list(dict.fromkeys(p for p in [lead.get("phone"), *(lead.get("phones") or [])] if p))


def is_stale(verification: dict) -> bool:
    checked = (verification or {}).get("checked_at")
    if not checked:
        return True
    from datetime import datetime, timedelta, timezone
    try:
        return datetime.fromisoformat(checked) < datetime.now(timezone.utc) - timedelta(days=RECHECK_DAYS)
    except ValueError:
        return True


async def verify_lead(lead: dict) -> dict:
    """Verifica correos y teléfonos del lead y guarda el resultado en `verification`."""
    region = default_region(lead)
    emails = list(dict.fromkeys(e.lower() for e in (lead.get("emails") or [])))
    results = await asyncio.gather(*(check_email(e) for e in emails))
    data = {"emails": dict(zip(emails, results)),
            "phones": {p: check_phone(p, region) for p in lead_phones(lead)},
            "checked_at": intelligence.now()}
    db.update("leads", lead["id"], {"verification": data})
    return data


async def verify_leads(ids, only_stale=False, sync_master=True) -> int:
    """Verifica varios leads (con caché DNS por dominio) y actualiza la base general."""
    from . import master
    done = 0
    for lid in ids:
        lead = db.one("SELECT * FROM leads WHERE id = ?", (lid,))
        if not lead or (only_stale and not is_stale(lead.get("verification"))):
            continue
        await verify_lead(lead)
        done += 1
        if sync_master:
            master.safe_sync(db.one("SELECT * FROM leads WHERE id = ?", (lid,)))
    return done


def summary(verification: dict) -> dict:
    """Resumen para etiquetas y para el nivel de confianza."""
    emails = (verification or {}).get("emails") or {}
    phones = (verification or {}).get("phones") or {}
    return {"email_ok": sum(v.get("status") in GOOD_EMAIL for v in emails.values()),
            "email_bad": sum(v.get("status") not in GOOD_EMAIL | {"unknown"} for v in emails.values()),
            "phone_ok": sum(v.get("status") in GOOD_PHONE for v in phones.values()),
            "phone_bad": sum(v.get("status") not in GOOD_PHONE for v in phones.values()),
            "mobile": any(v.get("type") in ("móvil", "fijo o móvil") for v in phones.values()),
            "checked": bool(emails or phones)}
