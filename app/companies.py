"""Empresas, registros de sedes y contactos profesionales con evidencia."""
import re
import unicodedata
from urllib.parse import urlparse, quote

from . import db, intelligence


def normalized(value):
    plain = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode().lower()
    return " ".join(re.findall(r"[a-z0-9]+", plain))


def domain(value):
    p = urlparse(value or "")
    host = (p.hostname or "").lower().removeprefix("www.")
    if not host or any(host == h or host.endswith("." + h) for h in (
        "instagram.com", "facebook.com", "linkedin.com", "google.com", "wa.me", "linktr.ee", "wixsite.com", "wordpress.com", "sites.google.com")):
        return ""
    return host


def linkedin_url(value, kind="company"):
    p = urlparse(value or "")
    host = (p.hostname or "").lower()
    if p.scheme != "https" or p.username or not (host == "linkedin.com" or host.endswith(".linkedin.com")):
        return ""
    match = re.fullmatch(r"/" + kind + r"/([A-Za-z0-9_%.-]+)/?", p.path)
    return "https://www.linkedin.com/" + kind + "/" + match.group(1).lower() + "/" if match else ""


def same_company(a, b):
    # Nunca agrupar solo por nombre, teléfono o dominio compartido.
    if intelligence.analyze(a)["data_quality"]["demo"] != intelligence.analyze(b)["data_quality"]["demo"]:
        return False
    al, bl = linkedin_url(a.get("linkedin")), linkedin_url(b.get("linkedin"))
    if al and bl and al != bl:
        return False
    if al and al == bl:
        return True
    ad, bd = domain(a.get("website")), domain(b.get("website"))
    if ad and ad == bd and normalized(a.get("name")) == normalized(b.get("name")):
        return True
    ak, bk = a.get("dedupe_key", ""), b.get("dedupe_key", "")
    if ak and ak == bk:
        return True
    return bool(normalized(a.get("name")) and normalized(a.get("address")) and normalized(a.get("city"))
                and all(normalized(a.get(k)) == normalized(b.get(k)) for k in ("name", "address", "city")))


def attach(lead):
    if lead.get("company_id"):
        return lead["company_id"]
    candidates = db.query("SELECT * FROM leads WHERE company_id IS NOT NULL AND id != ?", (lead["id"],))
    matches = {r["company_id"] for r in candidates if same_company(lead, r)}
    current = lead.get("company_id")
    # Mantener la asociación ante señales contradictorias; no fusionar empresas transitivamente.
    if current:
        return current
    if len(matches) == 1:
        cid = matches.pop()
    else:
        cid = db.insert("companies", {"name": lead.get("name") or "Empresa sin nombre",
            "grouping_note": "Coincidencia ambigua: revisar manualmente." if matches else "Registro original; coincidencias exactas y asociación manual."})
    db.update("leads", lead["id"], {"company_id": cid})
    return cid


def backfill():
    for lead in db.query("SELECT * FROM leads WHERE company_id IS NULL ORDER BY id"):
        attach(lead)


def context(cid):
    company = db.one("SELECT * FROM companies WHERE id=?", (cid,))
    records = db.query("SELECT * FROM leads WHERE company_id=? ORDER BY id", (cid,))
    return {**company, "names": list(dict.fromkeys([company["name"]] + [r["name"] for r in records if r.get("name")])),
            "domains": list(dict.fromkeys(domain(r.get("website")) for r in records if domain(r.get("website")))),
            "linkedin_urls": list(dict.fromkeys(linkedin_url(r.get("linkedin")) for r in records if linkedin_url(r.get("linkedin")))),
            "records": records}


def relation_matches(org, company):
    if isinstance(org, list):
        return any(relation_matches(o, company) for o in org)
    if isinstance(org, str):
        return normalized(org) in {normalized(n) for n in company["names"]}
    if not isinstance(org, dict):
        return False
    name = org.get("name") if isinstance(org.get("name"), str) else ""
    links = org.get("sameAs", [])
    links = list(links) if isinstance(links, list) else [links]
    links += [org.get("url", ""), org.get("@id", "")]
    org_domains = {domain(u) for u in links if isinstance(u, str) and domain(u)}
    org_linkedin = {linkedin_url(u) for u in links if isinstance(u, str) and linkedin_url(u)}
    if org_domains and company["domains"] and not org_domains.intersection(company["domains"]):
        return False
    if org_linkedin and company["linkedin_urls"] and not org_linkedin.intersection(company["linkedin_urls"]):
        return False
    return (bool(name) and normalized(name) in {normalized(n) for n in company["names"]}) or any(
        isinstance(u, str) and ((domain(u) and domain(u) in company["domains"])
                               or (linkedin_url(u) and linkedin_url(u) in company["linkedin_urls"])) for u in links)


def prioritize(contact, profile):
    role = normalized(contact.get("role"))
    owner = bool(re.search(r"\b(ceo|owner|founder|cofounder|fundador|fundadora|propietario|propietaria|dueno|duena|gerente general|director general|gerencia|presidente)\b", role))
    marketing = bool(re.search(r"\b(marketing|mercadeo|comunicacion|comunicaciones|cmo|seo|redes|social media)\b", role))
    digital = bool(re.search(r"\b(digital|web|tecnologia|technology|cto|cio|sistemas|ecommerce|e commerce|it)\b", role))
    management = bool(re.search(r"\b(gerente|director|directora|head|chief|manager|responsable|cto|cio|cmo|ceo)\b", role))
    fit = (profile in ("social", "seo") and marketing) or (profile == "web" and digital)
    score = 95 if fit and management else 90 if owner else 65 if fit else 60 if management else 45 if marketing or digital else 0
    if contact.get("relationship") in ("pending", "rejected"):
        score = 0
    return {**contact, "decision_priority": score,
            "decision_reason": "Vínculo pendiente o descartado: sin prioridad confirmada." if contact.get("relationship") in ("pending", "rejected") else "Cargo sin señales suficientes para priorizar." if score == 0 else
            "Encaje por cargo publicado/aportado; no confirma autoridad de compra ni empleo actual."}


def save_contact(cid, data, *, manual=False):
    url = linkedin_url(data.get("linkedin"), "in")
    key = "linkedin:" + url if url else "name-role:" + normalized(data.get("name")) + "|" + normalized(data.get("role"))
    old = db.one("SELECT * FROM company_contacts WHERE company_id=? AND identity_key=?", (cid, key))
    fields = {"name": (data.get("name") or "")[:200], "role": (data.get("role") or "")[:300], "linkedin": url,
              "relationship": data.get("relationship", "pending"), "review_data": data.get("review_data", {}),
              "emails": data.get("emails", []), "phones": data.get("phones", []), "evidence_records": data.get("evidence_records", [])}
    if old:
        for field in ("name", "role"):
            fields[field] = fields[field] or old[field]
        for field in ("emails", "phones"):
            fields[field] = list(dict.fromkeys(old[field] + fields[field]))
        fields["evidence_records"] = intelligence.merge_provenance({"records": old["evidence_records"]}, {"records": fields["evidence_records"]})["records"]
        # La decisión humana prevalece sobre una nueva extracción.
        if not manual and (old["relationship"] in ("confirmed", "rejected") or (old["relationship"] == "published" and fields["relationship"] == "pending")):
            fields["relationship"] = old["relationship"]
        db.update("company_contacts", old["id"], fields)
        return old["id"]
    return db.insert("company_contacts", {"company_id": cid, "identity_key": key, **fields})


def detail(cid, profile="general"):
    info = context(cid)
    contacts = [prioritize(c, profile) for c in db.query("SELECT * FROM company_contacts WHERE company_id=?", (cid,))]
    contacts.sort(key=lambda c: (c["decision_priority"], c["id"]), reverse=True)
    query = 'site:linkedin.com/in/ "' + info["name"] + '" ' + {"web": '(gerente OR marketing OR digital OR CTO)', "seo": '(gerente OR marketing OR SEO)', "social": '(gerente OR marketing OR redes)', "general": '(dueño OR fundador OR gerente OR CEO)'}.get(profile, 'gerente')
    return {**info, "contacts": contacts, "search_url": "https://www.google.com/search?q=" + quote(query)}
