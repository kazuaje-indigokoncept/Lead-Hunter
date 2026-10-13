"""Base general: un registro por negocio, alimentado por las campañas de todos los usuarios.

Cada lead real (no de demostración) se une a un registro maestro por claves exactas:
ficha de Google Maps, matrícula RUES, dominio web + nombre, página de LinkedIn,
nombre + dirección o teléfono + nombre. El registro se reconstruye a partir de todos
sus leads, de modo que siempre refleja el dato más reciente de cualquier campaña.

Solo los administradores acceden a la base. Las exclusiones (negocios que pidieron no
figurar) se respetan en cada exportación y borran los datos de contacto del registro.
"""
import csv
import io
import json
import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from . import auth, companies, db

SCHEMA = """
CREATE TABLE IF NOT EXISTS master_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL DEFAULT '',
    category TEXT, address TEXT, city TEXT,
    phone TEXT, phones TEXT NOT NULL DEFAULT '[]', whatsapp TEXT,
    emails TEXT NOT NULL DEFAULT '[]',
    website TEXT, instagram TEXT, facebook TEXT, tiktok TEXT, linkedin TEXT,
    rating REAL, reviews INTEGER, maps_url TEXT, lat REAL, lng REAL, business_status TEXT,
    extra TEXT NOT NULL DEFAULT '{}',
    sources TEXT NOT NULL DEFAULT '[]',
    quality INTEGER NOT NULL DEFAULT 0,
    lead_count INTEGER NOT NULL DEFAULT 0,
    campaign_count INTEGER NOT NULL DEFAULT 0,
    owner_count INTEGER NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT '',
    locked TEXT NOT NULL DEFAULT '[]',          -- campos corregidos a mano
    excluded INTEGER NOT NULL DEFAULT 0,
    first_seen TEXT, last_seen TEXT, verified_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS master_keys (
    key TEXT PRIMARY KEY,
    master_id INTEGER NOT NULL REFERENCES master_records(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS master_keys_record ON master_keys(master_id);
CREATE INDEX IF NOT EXISTS master_records_city ON master_records(city);
CREATE INDEX IF NOT EXISTS master_records_seen ON master_records(last_seen);
CREATE TABLE IF NOT EXISTS master_exclusions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    value TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    label TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(kind, value)
);
CREATE TABLE IF NOT EXISTS master_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    price TEXT NOT NULL DEFAULT '',
    filters TEXT NOT NULL DEFAULT '{}',
    include_personal INTEGER NOT NULL DEFAULT 0,
    record_count INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS master_delivery_items (
    delivery_id INTEGER NOT NULL REFERENCES master_deliveries(id) ON DELETE CASCADE,
    master_id INTEGER NOT NULL,
    PRIMARY KEY (delivery_id, master_id)
);
CREATE INDEX IF NOT EXISTS master_delivery_items_record ON master_delivery_items(master_id);
"""

IDENTITY = ("name", "category", "address", "city")        # primer valor visto (estable)
LATEST = ("phone", "whatsapp", "website", "instagram", "facebook", "tiktok", "linkedin",
          "rating", "reviews", "maps_url", "lat", "lng", "business_status")   # dato más reciente
CONTACT_FIELDS = ("phone", "phones", "whatsapp", "emails", "website", "instagram", "facebook",
                  "tiktok", "linkedin", "maps_url", "lat", "lng", "extra", "address")
EDITABLE = ("name", "category", "address", "city")
EXCLUSION_KINDS = {"record": "Registro", "domain": "Dominio web", "phone": "Teléfono",
                   "email": "Correo", "name": "Nombre del negocio"}

# Buzones de empresa: el resto de correos se trata como posible dato personal.
GENERIC_EMAILS = {
    "info", "informacion", "informes", "contacto", "contactenos", "contact", "contactus", "ventas", "sales",
    "hola", "hello", "hi", "admin", "administracion", "gerencia", "recepcion", "citas", "servicio",
    "servicioalcliente", "atencion", "atencionalcliente", "soporte", "support", "comercial", "marketing",
    "mercadeo", "facturacion", "pedidos", "reservas", "reservaciones", "office", "oficina", "consultas",
    "clientes", "rrhh", "talentohumano", "compras", "contabilidad", "tienda", "shop", "store", "mail",
    "email", "web", "webmaster", "prensa", "comunicaciones", "agenda", "general", "empresa", "negocios",
    "cotizaciones", "domicilios", "noreply", "no-reply", "secretaria", "direccion", "help", "ayuda",
}


def init():
    with db.connect() as c:
        c.executescript(SCHEMA)
        if "master_id" not in {r[1] for r in c.execute("PRAGMA table_info(leads)")}:
            c.execute("ALTER TABLE leads ADD COLUMN master_id INTEGER")
        c.execute("CREATE INDEX IF NOT EXISTS leads_master ON leads(master_id)")


# ---------------- reglas de identidad ----------------
def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def phone_digits(value):
    digits = re.sub(r"\D", "", str(value or ""))
    return digits[-10:] if len(digits) >= 7 else ""


def normalize_exclusion(kind, value):
    value = (value or "").strip()
    if kind == "domain":
        return companies.domain(value if "://" in value else "https://" + value)
    if kind == "phone":
        return phone_digits(value)
    if kind == "email":
        return value.lower() if "@" in value else ""
    if kind == "name":
        return companies.normalized(value)
    if kind == "record":
        return value if value.isdigit() else ""
    return ""


def email_kind(email):
    local = email.split("@")[0].lower()
    base = re.split(r"[._+\-0-9]", local)[0]
    return "empresa" if local in GENERIC_EMAILS or base in GENERIC_EMAILS else "personal"


def is_demo(lead):
    if ":demo:" in (lead.get("dedupe_key") or ""):
        return True
    prov = lead.get("provenance") or {}
    return any(r.get("verification") == "demo" for records in prov.values() for r in records)


def lead_phones(lead):
    return list(dict.fromkeys(p for p in [lead.get("phone"), *(lead.get("phones") or [])] if p))


def lead_keys(lead):
    keys = []
    dk = lead.get("dedupe_key") or ""
    if dk.startswith(("gmaps:", "rues:")) and "None" not in dk and not dk.endswith(":"):
        keys.append("dk:" + dk)
    name = companies.normalized(lead.get("name"))
    dom = companies.domain(lead.get("website"))
    if dom and name:
        keys.append(f"dom:{dom}|{name}")
    li = companies.linkedin_url(lead.get("linkedin"))
    if li:
        keys.append("li:" + li)
    address = companies.normalized(lead.get("address"))
    if name and address:
        keys.append(f"nad:{name}|{address}")
    if name:
        keys += [f"ph:{d}|{name}" for d in (phone_digits(p) for p in lead_phones(lead)) if d]
    return list(dict.fromkeys(keys))


def score(rec):
    s = 25 if rec.get("emails") else 0
    s += 20 if rec.get("whatsapp") else 0
    s += 15 if rec.get("phone") or rec.get("phones") else 0
    s += 10 if rec.get("website") else 0
    s += 10 if rec.get("instagram") or rec.get("facebook") else 0
    s += 10 if (rec.get("rating") or 0) >= 4 else 0
    s += 10 if (rec.get("reviews") or 0) >= 20 else 0
    return s


# ---------------- exclusiones ----------------
def exclusions():
    out = {}
    for r in db.query("SELECT kind, value FROM master_exclusions"):
        out.setdefault(r["kind"], set()).add(r["value"])
    return out


def excluded_by(mid, rec, keys, excl):
    """True si el registro coincide con alguna exclusión (por id, dominio, teléfono, correo o nombre)."""
    if not excl:
        return False
    domains = {companies.domain(rec.get("website"))} | {k[4:].split("|")[0] for k in keys if k.startswith("dom:")}
    phones = {phone_digits(p) for p in lead_phones(rec)} | {k[3:].split("|")[0] for k in keys if k.startswith("ph:")}
    return bool(
        str(mid) in excl.get("record", ())
        or (domains - {""}) & excl.get("domain", set())
        or (phones - {""}) & excl.get("phone", set())
        or {e.lower() for e in rec.get("emails") or []} & excl.get("email", set())
        or companies.normalized(rec.get("name")) in excl.get("name", set()))


# ---------------- sincronización ----------------
def rebuild(mid, excl=None):
    """Recalcula el registro maestro a partir de todos sus leads."""
    rec = db.one("SELECT * FROM master_records WHERE id = ?", (mid,))
    if not rec:
        return
    leads = db.query("""SELECT l.*, c.owner_id FROM leads l JOIN campaigns c ON c.id = l.campaign_id
                        WHERE l.master_id = ? ORDER BY l.id""", (mid,))
    keys = [r["key"] for r in db.query("SELECT key FROM master_keys WHERE master_id = ?", (mid,))]
    out = {}
    if leads:
        for field in IDENTITY:
            out[field] = next((l[field] for l in leads if l.get(field)), None)
        out["name"] = out["name"] or rec["name"]
        for field in LATEST:
            out[field] = next((l[field] for l in reversed(leads) if l.get(field) not in (None, "")), None)
        out["emails"] = list(dict.fromkeys(e.lower() for l in leads for e in (l.get("emails") or [])))
        out["phones"] = list(dict.fromkeys(p for l in leads for p in lead_phones(l)))
        extra = {}
        for l in leads:
            extra.update(l.get("extra") or {})
        out["extra"] = extra
        out["sources"] = sorted({l["source"] for l in leads if l.get("source")})
        out["lead_count"] = len(leads)
        out["campaign_count"] = len({l["campaign_id"] for l in leads})
        out["owner_count"] = len({l["owner_id"] for l in leads})
        seen = [r.get("observed_at") for l in leads for records in (l.get("provenance") or {}).values()
                for r in records if r.get("observed_at")]
        if seen:
            out["first_seen"] = min(min(seen), rec["first_seen"] or "9999")
            out["last_seen"] = max(max(seen), rec["last_seen"] or "")
        else:
            out["first_seen"] = rec["first_seen"] or now_iso()
            out["last_seen"] = rec["last_seen"] or now_iso()
        audits = [l["web_audit"].get("checked_at") for l in leads if (l.get("web_audit") or {}).get("checked_at")]
        out["verified_at"] = max(audits) if audits else rec["verified_at"]
    for field in rec["locked"] or []:
        out.pop(field, None)
    merged = {**rec, **out}
    excluded = excluded_by(mid, merged, keys, exclusions() if excl is None else excl)
    out["excluded"] = int(excluded)
    if excluded:
        # Quien pidió no figurar conserva solo nombre y ciudad para seguir reconociéndolo.
        for field in CONTACT_FIELDS:
            out[field] = [] if field in ("phones", "emails") else {} if field == "extra" else None
        merged = {**merged, **out}
    out["quality"] = 0 if excluded else score(merged)
    db.update("master_records", mid, out)


def sync_lead(lead):
    """Une un lead real a su registro maestro (creándolo si hace falta) y lo recalcula."""
    if not lead or is_demo(lead) or not (lead.get("name") or "").strip():
        return None
    keys = lead_keys(lead)
    mid = lead.get("master_id")
    if mid and not db.one("SELECT id FROM master_records WHERE id = ?", (mid,)):
        mid = None
    if not mid and keys:
        found = db.one(f"SELECT MIN(master_id) m FROM master_keys WHERE key IN ({','.join('?' * len(keys))})", tuple(keys))
        mid = found["m"] if found else None
    if not mid:
        mid = db.insert("master_records", {"name": lead["name"].strip(), "first_seen": now_iso(), "last_seen": now_iso()})
    for key in keys:
        db.execute("INSERT OR IGNORE INTO master_keys(key, master_id) VALUES (?, ?)", (key, mid))
    if lead.get("master_id") != mid:
        db.execute("UPDATE leads SET master_id = ? WHERE id = ?", (mid, lead["id"]))
    rebuild(mid)
    return mid


def safe_sync(lead):
    """La base general nunca debe interrumpir una campaña."""
    try:
        return sync_lead(lead)
    except Exception:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        return None


def backfill(all_leads=False):
    sql = "SELECT * FROM leads" + ("" if all_leads else " WHERE master_id IS NULL") + " ORDER BY id"
    count = 0
    for lead in db.query(sql):
        if sync_lead(lead):
            count += 1
    return count


def refresh_exclusions(candidates=None):
    excl = exclusions()
    ids = candidates if candidates is not None else [r["id"] for r in db.query("SELECT id FROM master_records")]
    for mid in ids:
        rebuild(mid, excl)


def candidates_for(kind, value):
    """Registros que una exclusión puede afectar, sin recorrer toda la base."""
    if kind == "record":
        return [int(value)]
    if kind in ("domain", "phone"):
        prefix = "dom:" if kind == "domain" else "ph:"
        return [r["master_id"] for r in db.query("SELECT DISTINCT master_id FROM master_keys WHERE key LIKE ?",
                                                 (f"{prefix}{value}|%",))]
    if kind == "email":
        return [r["id"] for r in db.query("SELECT id FROM master_records WHERE emails LIKE ?", (f'%"{value}"%',))]
    if kind == "name":
        first = value.split(" ")[0]
        return [r["id"] for r in db.query("SELECT id, name FROM master_records WHERE name LIKE ?", (f"%{first}%",))
                if companies.normalized(r["name"]) == value]
    return []


def merge(source_id, target_id):
    if source_id == target_id:
        raise HTTPException(400, "Elige un registro distinto.")
    src = db.one("SELECT * FROM master_records WHERE id = ?", (source_id,))
    dst = db.one("SELECT * FROM master_records WHERE id = ?", (target_id,))
    if not src or not dst:
        raise HTTPException(404, "Registro no encontrado")
    db.execute("UPDATE leads SET master_id = ? WHERE master_id = ?", (target_id, source_id))
    db.execute("UPDATE master_keys SET master_id = ? WHERE master_id = ?", (target_id, source_id))
    db.execute("INSERT OR IGNORE INTO master_delivery_items(delivery_id, master_id) "
               "SELECT delivery_id, ? FROM master_delivery_items WHERE master_id = ?", (target_id, source_id))
    db.execute("DELETE FROM master_delivery_items WHERE master_id = ?", (source_id,))
    db.execute("UPDATE master_exclusions SET value = ? WHERE kind = 'record' AND value = ?", (str(target_id), str(source_id)))
    notes = "\n".join(n for n in (dst["notes"], src["notes"]) if n)
    db.update("master_records", target_id, {"notes": notes, "first_seen": min(filter(None, [dst["first_seen"], src["first_seen"]]), default=None),
                                           "locked": sorted(set(dst["locked"]) | set(src["locked"]))})
    db.execute("DELETE FROM master_records WHERE id = ?", (source_id,))
    rebuild(target_id)


# ---------------- consultas ----------------
SORTS = {"recent": "last_seen DESC", "quality": "quality DESC, last_seen DESC", "name": "name COLLATE NOCASE",
         "rating": "rating DESC", "reviews": "reviews DESC", "sources": "lead_count DESC"}


class Filters(BaseModel):
    q: str = ""
    city: str = ""
    category: str = ""
    source: str = ""
    has_email: bool = False
    has_phone: bool = False
    has_wa: bool = False
    has_web: bool = False
    no_web: bool = False
    has_ig: bool = False
    min_rating: float = 0
    min_quality: int = 0
    updated_days: int = 0
    excluded: str = "hide"          # hide | only | all
    not_delivered_to: str = ""
    never_delivered: bool = False
    delivery_id: int = 0


def where(f: Filters):
    w, p = [], []
    if f.excluded == "only":
        w.append("excluded = 1")
    elif f.excluded != "all":
        w.append("excluded = 0")
    if f.q:
        w.append("(name LIKE ? OR category LIKE ? OR address LIKE ? OR city LIKE ? OR website LIKE ? OR emails LIKE ?)")
        p += [f"%{f.q}%"] * 6
    if f.city:
        w.append("city LIKE ?"); p.append(f"%{f.city}%")
    if f.category:
        w.append("category LIKE ?"); p.append(f"%{f.category}%")
    if f.source:
        w.append("sources LIKE ?"); p.append(f"%{f.source}%")
    if f.has_email:
        w.append("emails != '[]'")
    if f.has_phone:
        w.append("((phone IS NOT NULL AND phone != '') OR phones != '[]')")
    if f.has_wa:
        w.append("whatsapp IS NOT NULL AND whatsapp != ''")
    if f.has_web:
        w.append("website IS NOT NULL AND website != ''")
    if f.no_web:
        w.append("(website IS NULL OR website = '')")
    if f.has_ig:
        w.append("instagram IS NOT NULL AND instagram != ''")
    if f.min_rating:
        w.append("rating >= ?"); p.append(f.min_rating)
    if f.min_quality:
        w.append("quality >= ?"); p.append(f.min_quality)
    if f.updated_days:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=f.updated_days)).isoformat(timespec="seconds")
        w.append("last_seen >= ?"); p.append(cutoff)
    if f.not_delivered_to:
        w.append("""id NOT IN (SELECT i.master_id FROM master_delivery_items i JOIN master_deliveries d
                    ON d.id = i.delivery_id WHERE d.client = ? COLLATE NOCASE)"""); p.append(f.not_delivered_to.strip())
    if f.never_delivered:
        w.append("id NOT IN (SELECT master_id FROM master_delivery_items)")
    if f.delivery_id:
        w.append("id IN (SELECT master_id FROM master_delivery_items WHERE delivery_id = ?)"); p.append(f.delivery_id)
    return " AND ".join(w) or "1=1", p


def delivery_counts(ids):
    if not ids:
        return {}
    rows = db.query(f"SELECT master_id, COUNT(*) n FROM master_delivery_items WHERE master_id IN ({','.join('?' * len(ids))}) "
                    "GROUP BY master_id", tuple(ids))
    return {r["master_id"]: r["n"] for r in rows}


def people_for(mid, only_linked=True):
    """Personas de contacto de las empresas asociadas a los leads del registro."""
    rows = db.query("""SELECT DISTINCT cc.* FROM company_contacts cc JOIN leads l ON l.company_id = cc.company_id
                       WHERE l.master_id = ?""", (mid,))
    seen, out = set(), []
    for r in rows:
        if only_linked and r["relationship"] not in ("confirmed", "published"):
            continue
        key = r["linkedin"] or companies.normalized(r["name"]) + "|" + companies.normalized(r["role"])
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": r["name"], "role": r["role"], "linkedin": r["linkedin"], "emails": r["emails"],
                    "phones": r["phones"], "relationship": r["relationship"]})
    return out


EXPORT_COLS = [("id", "ID base"), ("name", "Nombre"), ("category", "Categoría"), ("city", "Ciudad"),
               ("address", "Dirección"), ("phone", "Teléfono"), ("other_phones", "Otros teléfonos"),
               ("whatsapp", "WhatsApp"), ("company_emails", "Correos de empresa"), ("website", "Sitio web"),
               ("instagram", "Instagram"), ("facebook", "Facebook"), ("tiktok", "TikTok"),
               ("linkedin", "LinkedIn empresa"), ("rating", "Rating"), ("reviews", "Reseñas"),
               ("business_status", "Estado del negocio"), ("maps_url", "Google Maps"), ("rues", "Datos RUES"),
               ("quality", "Completitud (0-100)"), ("first_seen", "Primera vez visto"),
               ("last_seen", "Último dato observado"), ("verified_at", "Web revisada")]
PERSONAL_COLS = [("personal_emails", "Otros correos (posible dato personal)"), ("people", "Personas de contacto")]


def export_rows(ids, include_personal):
    rows = []
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        recs = db.query(f"SELECT * FROM master_records WHERE excluded = 0 AND id IN ({','.join('?' * len(chunk))}) ORDER BY id", tuple(chunk))
        for r in recs:
            emails = r["emails"] or []
            row = {**r, "other_phones": ", ".join(p for p in r["phones"] if p != r["phone"]),
                   "company_emails": ", ".join(e for e in emails if email_kind(e) == "empresa"),
                   "rues": ", ".join(f"{k}: {v}" for k, v in (r["extra"] or {}).items())}
            if include_personal:
                row["personal_emails"] = ", ".join(e for e in emails if email_kind(e) == "personal")
                row["people"] = " | ".join(
                    ", ".join(x for x in (p["name"], p["role"], p["linkedin"], *p["emails"], *p["phones"]) if x)
                    for p in people_for(r["id"]))
            rows.append(row)
    return rows


def file_response(rows, include_personal, fmt, name):
    cols = EXPORT_COLS + (PERSONAL_COLS if include_personal else [])
    header = [h for _, h in cols]
    data = [["'" + v if isinstance(v, str) and v.lstrip().startswith(("=", "+", "-", "@")) else v
             for v in ("" if r.get(k) is None else r.get(k) for k, _ in cols)] for r in rows]
    if fmt == "csv":
        buf = io.StringIO()
        buf.write("﻿")
        w = csv.writer(buf, delimiter=";")
        w.writerow(header); w.writerows(data)
        return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                                 headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter
    wb = Workbook(); ws = wb.active; ws.title = "Base"
    ws.append(header)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF"); c.fill = PatternFill("solid", fgColor="1F3A5F")
    for row in data:
        ws.append(row)
    for i in range(len(cols)):
        ws.column_dimensions[get_column_letter(i + 1)].width = 12 if i == 0 else 28
    ws.freeze_panes = "C2"; ws.auto_filter.ref = ws.dimensions
    out = io.BytesIO(); wb.save(out); out.seek(0)
    return StreamingResponse(out, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f'attachment; filename="{name}.xlsx"'})


def ids_for(f: Filters, sort="recent", limit=0):
    w, p = where(f)
    sql = f"SELECT id FROM master_records WHERE {w} ORDER BY {SORTS.get(sort, SORTS['recent'])}"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [r["id"] for r in db.query(sql, tuple(p))]


def slug(text):
    return "".join(ch if ch.isalnum() else "_" for ch in text)[:40].strip("_") or "base"


# ---------------- API (solo administradores) ----------------
router = APIRouter(prefix="/api/master", dependencies=[Depends(auth.require_admin)])


def filters_dep(q: str = "", city: str = "", category: str = "", source: str = "", has_email: bool = False,
                has_phone: bool = False, has_wa: bool = False, has_web: bool = False, no_web: bool = False,
                has_ig: bool = False, min_rating: float = Query(0, ge=0, le=5), min_quality: int = Query(0, ge=0, le=100),
                updated_days: int = Query(0, ge=0, le=3650), excluded: str = "hide", not_delivered_to: str = "",
                never_delivered: bool = False, delivery_id: int = 0):
    if excluded not in ("hide", "only", "all"):
        raise HTTPException(422, "Valor de excluidos inválido")
    return Filters(**locals())


def record_or_404(mid):
    rec = db.one("SELECT * FROM master_records WHERE id = ?", (mid,))
    if not rec:
        raise HTTPException(404, "Registro no encontrado")
    return rec


@router.get("/stats")
def stats():
    s = db.one("""SELECT SUM(excluded = 0) total, SUM(excluded = 1) excluded,
        SUM(excluded = 0 AND emails != '[]') email,
        SUM(excluded = 0 AND ((phone IS NOT NULL AND phone != '') OR phones != '[]')) phone,
        SUM(excluded = 0 AND whatsapp IS NOT NULL AND whatsapp != '') wa,
        SUM(excluded = 0 AND website IS NOT NULL AND website != '') web,
        COUNT(DISTINCT CASE WHEN excluded = 0 THEN city END) cities,
        ROUND(AVG(CASE WHEN excluded = 0 THEN quality END), 0) avg_quality FROM master_records""")
    d = db.one("SELECT COUNT(*) deliveries, COUNT(DISTINCT client) clients FROM master_deliveries")
    sold = db.one("SELECT COUNT(DISTINCT master_id) n FROM master_delivery_items")["n"]
    pending = db.one("SELECT COUNT(*) n FROM leads WHERE master_id IS NULL")["n"]
    return {**{k: (v or 0) for k, v in s.items()}, **d, "delivered_records": sold, "unlinked_leads": pending}


@router.get("/facets")
def facets():
    cities = db.query("SELECT city value, COUNT(*) n FROM master_records WHERE excluded = 0 AND city IS NOT NULL AND city != '' "
                      "GROUP BY city ORDER BY n DESC LIMIT 200")
    cats = db.query("SELECT category value, COUNT(*) n FROM master_records WHERE excluded = 0 AND category IS NOT NULL "
                    "AND category != '' GROUP BY category ORDER BY n DESC LIMIT 200")
    clients = db.query("SELECT client value, COUNT(*) n, MAX(created_at) last FROM master_deliveries GROUP BY client ORDER BY last DESC")
    return {"cities": cities, "categories": cats, "clients": clients}


@router.get("/records")
def list_records(f: Filters = Depends(filters_dep), sort: str = "recent",
                 page: int = Query(1, ge=1), per_page: int = Query(50, ge=1, le=200)):
    w, p = where(f)
    total = db.one(f"SELECT COUNT(*) n FROM master_records WHERE {w}", tuple(p))["n"]
    rows = db.query(f"SELECT * FROM master_records WHERE {w} ORDER BY {SORTS.get(sort, SORTS['recent'])} "
                    f"LIMIT ? OFFSET ?", tuple(p) + (per_page, (page - 1) * per_page))
    counts = delivery_counts([r["id"] for r in rows])
    for r in rows:
        r["delivered"] = counts.get(r["id"], 0)
        r["email_kinds"] = {e: email_kind(e) for e in r["emails"]}
    return {"total": total, "page": page, "per_page": per_page, "items": rows}


@router.get("/records/{mid}")
def get_record(mid: int):
    rec = record_or_404(mid)
    rec["email_kinds"] = {e: email_kind(e) for e in rec["emails"]}
    rec["leads"] = db.query("""SELECT l.id, l.name, l.source, l.city, l.created_at, l.campaign_id,
        c.name campaign_name, u.username owner_name FROM leads l JOIN campaigns c ON c.id = l.campaign_id
        LEFT JOIN users u ON u.id = c.owner_id WHERE l.master_id = ? ORDER BY l.id DESC""", (mid,))
    rec["people"] = people_for(mid, only_linked=False)
    rec["deliveries"] = db.query("""SELECT d.id, d.client, d.created_at FROM master_deliveries d
        JOIN master_delivery_items i ON i.delivery_id = d.id WHERE i.master_id = ? ORDER BY d.id DESC""", (mid,))
    keys = [r["key"] for r in db.query("SELECT key FROM master_keys WHERE master_id = ?", (mid,))]
    rec["match_keys"] = keys
    return rec


class RecordPatch(BaseModel):
    name: str | None = Field(None, max_length=200)
    category: str | None = Field(None, max_length=200)
    address: str | None = Field(None, max_length=300)
    city: str | None = Field(None, max_length=120)
    notes: str | None = Field(None, max_length=4000)
    unlock: bool = False


@router.patch("/records/{mid}")
def patch_record(mid: int, body: RecordPatch):
    rec = record_or_404(mid)
    upd, locked = {}, set(rec["locked"] or [])
    if body.unlock:
        locked = set()
    for field in EDITABLE:
        value = getattr(body, field)
        if value is not None and value.strip() != (rec[field] or ""):
            if field == "name" and not value.strip():
                raise HTTPException(400, "El nombre no puede quedar vacío")
            upd[field] = value.strip()
            locked.add(field)
    if body.notes is not None:
        upd["notes"] = body.notes
    upd["locked"] = sorted(locked)
    db.update("master_records", mid, upd)
    rebuild(mid)
    return get_record(mid)


class MergeIn(BaseModel):
    into: int = Field(ge=1)


@router.post("/records/{mid}/merge")
def merge_record(mid: int, body: MergeIn):
    merge(mid, body.into)
    return get_record(body.into)


class ExcludeIn(BaseModel):
    reason: str = Field("", max_length=500)


def add_exclusion(kind, value, reason, user, label=""):
    """Guarda la exclusión y devuelve los registros a recalcular (None si el valor no es válido)."""
    value = normalize_exclusion(kind, value)
    if not value:
        return None
    db.execute("INSERT OR IGNORE INTO master_exclusions(kind, value, reason, label, created_by) VALUES (?,?,?,?,?)",
               (kind, value, reason, label, user))
    return candidates_for(kind, value)


@router.post("/records/{mid}/exclude")
def exclude_record(mid: int, body: ExcludeIn, request: Request):
    rec = record_or_404(mid)
    user = request.state.user["username"]
    label = rec["name"]
    affected = set(add_exclusion("record", str(mid), body.reason, user, label) or [])
    # También dominio y teléfonos, para reconocerlo aunque aparezca con otra ficha.
    if companies.domain(rec.get("website")):
        affected |= set(add_exclusion("domain", rec["website"], body.reason, user, label) or [])
    for phone in lead_phones(rec):
        affected |= set(add_exclusion("phone", phone, body.reason, user, label) or [])
    refresh_exclusions(sorted(affected))
    return get_record(mid)


@router.post("/records/{mid}/include")
def include_record(mid: int):
    record_or_404(mid)
    keys = [r["key"] for r in db.query("SELECT key FROM master_keys WHERE master_id = ?", (mid,))]
    domains = [k[4:].split("|")[0] for k in keys if k.startswith("dom:")]
    phones = [k[3:].split("|")[0] for k in keys if k.startswith("ph:")]
    db.execute("DELETE FROM master_exclusions WHERE kind = 'record' AND value = ?", (str(mid),))
    for kind, values in (("domain", domains), ("phone", phones)):
        for v in values:
            db.execute("DELETE FROM master_exclusions WHERE kind = ? AND value = ?", (kind, v))
    refresh_exclusions([r["id"] for r in db.query("SELECT id FROM master_records WHERE excluded = 1")])
    rec = record_or_404(mid)
    if rec["excluded"]:
        raise HTTPException(409, "Sigue excluido por otra regla (correo o nombre). Revísala en Exclusiones.")
    return get_record(mid)


@router.post("/resync")
def resync():
    linked = backfill(all_leads=True)
    refresh_exclusions(None)
    return {"linked_leads": linked, **stats()}


@router.get("/export")
def export(f: Filters = Depends(filters_dep), fmt: str = "xlsx", include_personal: bool = False,
           sort: str = "recent", limit: int = Query(0, ge=0, le=1_000_000)):
    rows = export_rows(ids_for(f, sort, limit), include_personal)
    return file_response(rows, include_personal, fmt, "base_general")


class DeliveryIn(BaseModel):
    client: str = Field(min_length=1, max_length=200)
    notes: str = Field("", max_length=2000)
    price: str = Field("", max_length=100)
    include_personal: bool = False
    skip_delivered: bool = True
    limit: int = Field(0, ge=0, le=1_000_000)
    sort: str = "quality"
    filters: dict = Field(default_factory=dict)


@router.post("/deliveries")
def create_delivery(body: DeliveryIn, request: Request):
    try:
        f = Filters(**{k: v for k, v in body.filters.items() if k in Filters.model_fields})
    except Exception:  # noqa: BLE001
        raise HTTPException(422, "Filtros inválidos")
    f.excluded = "hide"
    if body.skip_delivered:
        f.not_delivered_to = body.client.strip()
    ids = ids_for(f, body.sort, body.limit)
    if not ids:
        raise HTTPException(400, "No hay registros para entregar con estos filtros.")
    did = db.insert("master_deliveries", {"client": body.client.strip(), "notes": body.notes, "price": body.price,
                                          "filters": f.model_dump(), "include_personal": int(body.include_personal),
                                          "record_count": len(ids), "created_by": request.state.user["username"]})
    with db._lock, db.connect() as c:
        c.executemany("INSERT OR IGNORE INTO master_delivery_items(delivery_id, master_id) VALUES (?, ?)",
                      [(did, mid) for mid in ids])
    return db.one("SELECT * FROM master_deliveries WHERE id = ?", (did,))


@router.get("/deliveries")
def list_deliveries():
    return db.query("SELECT * FROM master_deliveries ORDER BY id DESC")


@router.get("/deliveries/{did}/download")
def download_delivery(did: int, fmt: str = "xlsx"):
    d = db.one("SELECT * FROM master_deliveries WHERE id = ?", (did,))
    if not d:
        raise HTTPException(404, "Entrega no encontrada")
    ids = [r["master_id"] for r in db.query("SELECT master_id FROM master_delivery_items WHERE delivery_id = ? ORDER BY master_id", (did,))]
    rows = export_rows(ids, bool(d["include_personal"]))
    return file_response(rows, bool(d["include_personal"]), fmt, f"entrega_{did}_{slug(d['client'])}")


@router.delete("/deliveries/{did}")
def delete_delivery(did: int):
    if not db.one("SELECT id FROM master_deliveries WHERE id = ?", (did,)):
        raise HTTPException(404, "Entrega no encontrada")
    db.execute("DELETE FROM master_delivery_items WHERE delivery_id = ?", (did,))
    db.execute("DELETE FROM master_deliveries WHERE id = ?", (did,))
    return {"ok": True}


@router.get("/exclusions")
def list_exclusions():
    rows = db.query("SELECT * FROM master_exclusions ORDER BY id DESC")
    for r in rows:
        r["kind_label"] = EXCLUSION_KINDS.get(r["kind"], r["kind"])
    return rows


class ExclusionIn(BaseModel):
    kind: str
    value: str = Field(min_length=1, max_length=300)
    reason: str = Field("", max_length=500)


@router.post("/exclusions")
def create_exclusion(body: ExclusionIn, request: Request):
    if body.kind not in EXCLUSION_KINDS or body.kind == "record":
        raise HTTPException(422, "Tipo de exclusión inválido")
    affected = add_exclusion(body.kind, body.value, body.reason, request.state.user["username"], body.value.strip())
    if affected is None:
        raise HTTPException(400, "Valor no válido para ese tipo de exclusión")
    refresh_exclusions(affected)
    return {"ok": True, "excluded": stats()["excluded"]}


@router.delete("/exclusions/{eid}")
def delete_exclusion(eid: int):
    if not db.one("SELECT id FROM master_exclusions WHERE id = ?", (eid,)):
        raise HTTPException(404, "Exclusión no encontrada")
    db.execute("DELETE FROM master_exclusions WHERE id = ?", (eid,))
    refresh_exclusions([r["id"] for r in db.query("SELECT id FROM master_records WHERE excluded = 1")])
    return {"ok": True}
