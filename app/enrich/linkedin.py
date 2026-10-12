"""Consulta opcional de páginas de empresa públicas accesibles, sin sesión."""
from urllib.parse import urlparse

import httpx
from . import website, people
from .. import companies, db
from ..intelligence import evidence, now


async def lookup(client, url):
    audit = {"status": "not_checked", "checked_at": now(), "url": url}
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    if not (host == "linkedin.com" or host.endswith(".linkedin.com")) or parsed.scheme != "https" or parsed.username:
        return {"linkedin_data": {**audit, "status": "invalid_url"}}
    if not parsed.path.startswith("/company/"):
        return {"linkedin_data": {**audit, "status": "personal_profile", "note": "Solo se consultan páginas de empresa, no perfiles personales."}}
    try:
        response, final, error = await website.fetch_page(client, url)
        audit["url"] = final
        if error:
            return {"linkedin_data": {**audit, "status": error}}
        if response.status_code in (401, 403, 429, 999):
            return {"linkedin_data": {**audit, "status": "access_restricted", "status_code": response.status_code}}
        if response.status_code != 200:
            return {"linkedin_data": {**audit, "status": "http_error", "status_code": response.status_code}}
        final_host = urlparse(final).hostname or ""
        if not (final_host == "linkedin.com" or final_host.endswith(".linkedin.com")) or not urlparse(final).path.startswith("/company/") or any(s in response.text.lower() for s in ("authwall", "security verification", "captcha")):
            return {"linkedin_data": {**audit, "status": "login_required"}}
        parser = website.PageParser()
        parser.feed(response.text)
        contacts = website.extract(response.text, final)
        emails = website._clean_emails(contacts["emails"], "")[:5]
        phones = contacts["phones"][:5]
        # No se interpreta una página vacía o renderizada con JS como una ficha completa.
        audit.update(status="public_page", title=parser.title.strip(), emails=emails, phones=phones,
                     note="Información pública parcial. No valida identidad, entrega de correo ni cuenta de WhatsApp.")
        out = {"linkedin_data": audit, "emails": emails, "phones": phones, "provenance": {},
               "people_candidates": people.extract(response.text, final, "linkedin_public")}
        for field, values in (("emails", emails), ("phones", phones)):
            out["provenance"][field] = [evidence(v, "linkedin_public", final) for v in values]
        if phones:
            out["phone_web"] = phones[0]
            out["provenance"]["phone"] = [evidence(phones[0], "linkedin_public", final)]
        return out
    except httpx.HTTPError:
        return {"linkedin_data": {**audit, "status": "network_error"}}


async def review_person(client, contact, company):
    url = companies.linkedin_url(contact.get("linkedin"), "in")
    audit = {"status": "not_checked", "url": url, "checked_at": now()}
    def failed(status):
        db.update("company_contacts", contact["id"], {"review_data": {**audit, "status": status}})
        return status
    if not url:
        return failed("invalid_url")
    try:
        response, final, error = await website.fetch_page(client, url)
        audit["url"] = final
        if error:
            return failed(error)
        audit["status_code"] = response.status_code
        if response.status_code in (401, 403, 429, 999):
            return failed("access_restricted")
        if response.status_code != 200:
            return failed("http_error")
        if companies.linkedin_url(final, "in") != url or any(s in response.text.lower() for s in ("authwall", "security verification", "captcha")):
            return failed("login_required")
        if "html" not in response.headers.get("content-type", ""):
            return failed("not_html")
        candidates = people.extract(response.text, final, "linkedin_public")
        candidates = [p for p in candidates if p.get("linkedin") == url and p.get("organization") and companies.relation_matches(p["organization"], company)]
        if not candidates:
            return failed("relationship_unconfirmed")
        candidate = candidates[0]
        companies.save_contact(company["id"], {**candidate, "relationship": "published",
            "review_data": {**audit, "status": "public_page"}})
        return "public_page"
    except httpx.HTTPError:
        return failed("network_error")


async def review_company_people(client, cid, *, demo=False, check_stop=None, discover=True):
    import asyncio
    company = companies.context(cid)
    summary = []
    if demo:
        companies.save_contact(cid, {"name": "Ana Demo", "role": "Directora de marketing", "relationship": "published",
            "evidence_records": [evidence("Contacto ficticio de demostración", "demo", "", verification="demo", confidence="baja")]})
        return {"status": "demo", "reviewed": 0, "results": []}
    # Primero leer la página de la empresa; luego solo perfiles ya vinculados a sus fuentes.
    for url in (company["linkedin_urls"][:3] if discover else []):
        if check_stop:
            check_stop()
        data = await lookup(client, url)
        people.store(cid, data.get("people_candidates", []))
        summary.append({"url": url, "status": data["linkedin_data"]["status"]})
        if data["linkedin_data"].get("status_code") == 429:
            return {"status": "access_restricted", "reviewed": 0, "results": summary}
        await asyncio.sleep(2)
    contacts = db.query("SELECT * FROM company_contacts WHERE company_id=? AND linkedin!='' AND relationship!='rejected' ORDER BY id LIMIT 15", (cid,))
    count = 0
    for contact in contacts:
        if check_stop:
            check_stop()
        status = await review_person(client, contact, company)
        count += 1
        summary.append({"url": contact["linkedin"], "status": status})
        updated = db.one("SELECT review_data FROM company_contacts WHERE id=?", (contact["id"],))
        if updated["review_data"].get("status_code") == 429:
            return {"status": "access_restricted", "reviewed": count, "results": summary}
        await asyncio.sleep(2)
    return {"status": "reviewed" if count else "no_candidates", "reviewed": count, "results": summary}
