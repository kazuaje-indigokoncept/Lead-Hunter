"""Evidencia por dato y prioridades comerciales explicables, sin inferir ausencias."""
from datetime import datetime, timezone

PROFILES = {"general": "Prospección general", "web": "Diseño web", "seo": "SEO local", "social": "Gestión de redes"}
CONTACTS = {"website": "Web", "emails": "Correo", "phone": "Teléfono", "whatsapp": "WhatsApp",
            "instagram": "Instagram", "facebook": "Facebook", "tiktok": "TikTok", "linkedin": "LinkedIn"}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def evidence(value, source, url, *, verification="publicado", confidence="media", observed_at=None):
    return {"value": value, "source": source, "url": url, "observed_at": observed_at or now(),
            "verification": verification, "confidence": confidence}


def merge_provenance(current, added):
    out = {k: list(v) for k, v in (current or {}).items()}
    for field, records in (added or {}).items():
        bucket = out.setdefault(field, [])
        for record in records:
            bucket[:] = [r for r in bucket if (r.get("value"), r.get("source"), r.get("url")) !=
                        (record.get("value"), record.get("source"), record.get("url"))]
            bucket.append(record)
        out[field] = bucket[-30:]
    return out


def initial_provenance(lead, source, demo=False):
    url = lead.get("maps_url") or ""
    out = {}
    for field in (*CONTACTS, "name", "address", "category", "rating", "reviews"):
        values = lead.get(field)
        values = values if isinstance(values, list) else [values]
        for value in values:
            if value is None or value == "":
                continue
            verification = "demo" if demo else "inferido" if field == "whatsapp" else "publicado"
            out.setdefault(field, []).append(evidence(value, "demo" if demo else source, url,
                verification=verification, confidence="baja" if verification != "publicado" else "media"))
    return out


def analyze(lead, profile="general"):
    profile = profile if profile in PROFILES else "general"
    audit = lead.get("web_audit") or {}
    provenance = lead.get("provenance") or {}
    checked = audit.get("status") == "ok"
    maps_checked = any(r.get("source", "").startswith("google_maps") or r.get("source") == "demo"
                       for records in provenance.values() for r in records)
    opportunities = []
    def opportunity(key, title, explanation, profiles, url=""):
        opportunities.append({"key": key, "title": title, "reason": explanation,
                              "profiles": profiles, "url": url})
    if not lead.get("website") and maps_checked:
        opportunity("no_website", "Web no encontrada en Maps", "La ficha consultada no publicó una web; comprobar antes de ofrecer una nueva.", ["web"], lead.get("maps_url", ""))
    if audit.get("status") == "http_error" and audit.get("status_code", 0) >= 400:
        opportunity("web_error", "Web con error HTTP", f"La web respondió HTTP {audit['status_code']} durante la revisión.", ["web"], audit.get("resolved_url", ""))
    if checked:
        if audit.get("https") is False:
            opportunity("no_https", "Web sin HTTPS observado", "La página final se cargó mediante HTTP.", ["web"], audit.get("resolved_url", ""))
        for key, title, description, profiles in (
            ("has_contact_form", "Formulario de contacto no encontrado", "No se detectó un formulario de contacto en las páginas visitadas.", ["web"]),
            ("has_viewport", "Etiqueta de adaptación móvil no encontrada", "No se encontró meta viewport en la portada; no confirma por sí solo problemas en móviles.", ["web"]),
            ("has_meta_description", "Descripción SEO no encontrada", "No se encontró meta description en la portada.", ["seo"]),
            ("has_title", "Título de página no encontrado", "La portada no publica una etiqueta title con contenido.", ["seo"]),
        ):
            if audit.get(key) is False:
                opportunity(key, title, description, profiles, audit.get("resolved_url", ""))
        if not lead.get("instagram") and not lead.get("facebook"):
            opportunity("no_social", "Redes principales no encontradas en la web", "Las páginas visitadas no enlazan Instagram ni Facebook; no demuestra ausencia de perfiles.", ["social"], audit.get("resolved_url", ""))
    if lead.get("reviews") is not None and lead["reviews"] < 20:
        opportunity("few_reviews", "Pocas reseñas observadas", f"La fuente consultada publicó {lead['reviews']} reseñas.", ["seo"], lead.get("maps_url", ""))
    if lead.get("rating") is not None and lead["rating"] < 4:
        opportunity("low_rating", "Valoración inferior a 4", f"Valoración publicada: {lead['rating']}/5. Revisar el contexto de las reseñas.", ["seo"], lead.get("maps_url", ""))

    tags = []
    for field, label in CONTACTS.items():
        value = lead.get(field)
        records = provenance.get(field, [])
        inferred = field == "whatsapp" and bool(value) and not any(r.get("value") == value and r.get("verification") == "publicado" for r in records)
        if value:
            state = "inferred" if inferred else "present"
            title = "WhatsApp posible" if inferred else f"Con {label}"
            detail = "Enlace construido desde un teléfono; no confirma cuenta de WhatsApp." if inferred else "Dato encontrado; publicación no equivale a contacto validado."
        else:
            searched = maps_checked if field in ("website", "phone") else checked
            state = "missing" if searched else "unknown"
            title = f"{label} no {'encontrada' if field == 'website' else 'encontrado'}" if searched else f"{label} sin revisar"
            detail = "No encontrado en las fuentes revisadas; puede existir en otras." if searched else "Sin revisión suficiente para afirmar ausencia."
        tags.append({"key": field, "label": title, "state": state, "detail": detail})

    # Contactabilidad, encaje con el servicio y actividad son dimensiones distintas.
    breakdown = []
    def points(label, amount):
        if amount:
            breakdown.append({"reason": label, "points": amount})
    points("Correo disponible", 20 if lead.get("emails") else 0)
    points("Teléfono disponible", 15 if lead.get("phone") else 0)
    points("WhatsApp enlazado en una fuente", 10 if any(t["key"] == "whatsapp" and t["state"] == "present" for t in tags) else 0)
    points("Actividad: al menos 20 reseñas", 10 if (lead.get("reviews") or 0) >= 20 else 0)
    relevant = opportunities if profile == "general" else [o for o in opportunities if profile in o["profiles"]]
    weights = {"web": {"no_website": 40, "web_error": 35, "no_https": 15, "has_contact_form": 15, "has_viewport": 10},
               "seo": {"has_meta_description": 25, "has_title": 25, "few_reviews": 20, "low_rating": 15},
               "social": {"no_social": 40}}
    fit = min(45, sum(weights.get(profile, {}).get(o["key"], 0) for o in relevant))
    if profile == "general":
        fit = min(45, (10 if lead.get("website") else 0) + (15 if lead.get("instagram") or lead.get("linkedin") else 0) + (20 if checked else 0))
    points(f"Encaje: {PROFILES[profile]}", fit)
    priority = min(100, sum(b["points"] for b in breakdown))
    known = [(field, value) for field in CONTACTS for value in
             (lead.get(field) if isinstance(lead.get(field), list) else [lead.get(field)]) if value]
    evidenced = sum(any(r.get("value") == value for r in provenance.get(field, [])) for field, value in known)
    quality = round(100 * evidenced / len(known)) if known else 0
    demo = any(r.get("verification") == "demo" for records in provenance.values() for r in records)
    return {"sales_profile": profile, "sales_profile_label": PROFILES[profile], "opportunity_score": priority,
            "score_breakdown": breakdown, "opportunities": opportunities, "relevant_opportunities": relevant,
            "tags": tags, "data_quality": {"evidence_coverage": quality, "demo": demo,
            "label": "Datos de demostración" if demo else "Con evidencia" if quality == 100 and known else "Evidencia parcial" if evidenced else "Sin trazabilidad",
            "email_status": "Detectado, sin validar entrega" if lead.get("emails") else "No encontrado" if checked else "Sin revisar",
            "note": "La cobertura mide procedencia documentada, no exactitud ni entrega de correos."}}
