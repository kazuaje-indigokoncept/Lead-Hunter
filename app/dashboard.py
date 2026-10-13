"""Resumen de la pantalla de inicio: cada usuario ve lo suyo; el administrador, todo y la base."""
from collections import Counter
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request

from . import db, jobs

router = APIRouter(prefix="/api")
DAYS = 30
STATUSES = ("nuevo", "contactado", "interesado", "descartado")


def _days(tz):
    today = datetime.now(tz).date()
    return [today - timedelta(days=i) for i in range(DAYS - 1, -1, -1)]


def _series(stamps, tz, utc=False):
    """Cuenta por día local. Los leads guardan hora local del servidor; la base, UTC."""
    counts = Counter()
    for s in stamps:
        if not s:
            continue
        try:
            dt = datetime.fromisoformat(s.replace(" ", "T"))
        except ValueError:
            continue
        if dt.tzinfo is None and utc:
            dt = dt.replace(tzinfo=timezone.utc)
        counts[dt.astimezone(tz).date()] += 1      # naive = hora local del servidor
    return [{"day": d.isoformat(), "n": counts.get(d, 0)} for d in _days(tz)]


@router.get("/dashboard")
def dashboard(request: Request):
    user = request.state.user
    admin = bool(user["is_admin"])
    tz = jobs.app_tz()
    scope = "1=1" if admin else "c.owner_id = ?"
    p = () if admin else (user["id"],)
    cutoff = (datetime.now() - timedelta(days=DAYS + 1)).strftime("%Y-%m-%d %H:%M:%S")

    stamps = [r["created_at"] for r in db.query(
        f"SELECT l.created_at FROM leads l JOIN campaigns c ON c.id = l.campaign_id WHERE {scope} AND l.created_at >= ?",
        p + (cutoff,))]
    daily = _series(stamps, tz)
    leads_today = daily[-1]["n"]
    last7, prev7 = sum(d["n"] for d in daily[-7:]), sum(d["n"] for d in daily[-14:-7])

    totals = db.one(f"""SELECT COUNT(*) total, SUM(l.emails != '[]') email,
        SUM(l.whatsapp IS NOT NULL AND l.whatsapp != '') wa, SUM(l.phone IS NOT NULL AND l.phone != '') phone
        FROM leads l JOIN campaigns c ON c.id = l.campaign_id WHERE {scope}""", p)
    pipeline = {r["status"]: r["n"] for r in db.query(
        f"SELECT l.status, COUNT(*) n FROM leads l JOIN campaigns c ON c.id = l.campaign_id WHERE {scope} GROUP BY l.status", p)}
    categories = db.query(f"""SELECT l.category name, COUNT(*) n FROM leads l JOIN campaigns c ON c.id = l.campaign_id
        WHERE {scope} AND l.category IS NOT NULL AND l.category != '' GROUP BY l.category ORDER BY n DESC LIMIT 6""", p)

    camps = db.query(f"""SELECT c.*, (SELECT username FROM users u WHERE u.id = c.owner_id) owner_name,
        (SELECT COUNT(*) FROM leads l WHERE l.campaign_id = c.id) leads FROM campaigns c WHERE {scope} ORDER BY c.id DESC""", p)
    day = datetime.now(tz).date().isoformat()
    upcoming, recent = [], []
    for c in camps:
        sch = jobs.schedule_of(c)
        item = {"id": c["id"], "name": c["name"], "owner_name": c["owner_name"], "status": c["status"],
                "leads": c["leads"], "running": c["id"] in jobs.RUNNING, "next_run_at": c["next_run_at"],
                "used_today": jobs.used_today(c, day), "daily_profiles": sch["daily_profiles"],
                "window_start": sch["window_start"], "window_end": sch["window_end"], "scheduled": sch["enabled"],
                "batch_cursor": c["batch_cursor"] or 0, "total_combos": len(jobs.combos_of(c))}
        if sch["enabled"] and (c["status"] in ("programada", "en curso") or item["running"]):
            upcoming.append(item)
        if len(recent) < 5:
            recent.append(item)
    upcoming.sort(key=lambda x: (not x["running"], x["next_run_at"] or 9e18))
    own_used = sum(jobs.used_today(c, day) for c in camps)

    out = {
        "admin": admin, "timezone": str(tz), "today": day,
        "kpis": {"leads_total": totals["total"] or 0, "leads_today": leads_today, "leads_7d": last7, "leads_prev_7d": prev7,
                 "with_email": totals["email"] or 0, "with_wa": totals["wa"] or 0, "with_phone": totals["phone"] or 0,
                 "campaigns": len(camps), "campaigns_active": len(upcoming),
                 "profiles_today": jobs.server_used_today(day) if admin else own_used, "profiles_cap": jobs.global_cap()},
        "daily": daily,
        "pipeline": [{"status": s, "n": pipeline.get(s, 0)} for s in STATUSES],
        "categories": categories,
        "upcoming": upcoming[:6],
        "recent": recent,
    }
    if admin:
        m = db.one("SELECT SUM(excluded = 0) total, SUM(excluded = 0 AND emails != '[]') email FROM master_records")
        created = [r["created_at"] for r in db.query("SELECT created_at FROM master_records WHERE excluded = 0")]
        growth = _series(created, tz, utc=True)
        before = (m["total"] or 0) - sum(g["n"] for g in growth)
        running = before
        for g in growth:
            running += g["n"]
            g["total"] = running
        out["master"] = {"total": m["total"] or 0, "with_email": m["email"] or 0, "growth": growth,
                         "new_7d": sum(g["n"] for g in growth[-7:]),
                         "deliveries": db.one("SELECT COUNT(*) n FROM master_deliveries")["n"]}
        seven = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        out["team"] = db.query("""SELECT u.username, u.active, (SELECT COUNT(*) FROM leads l JOIN campaigns c ON c.id = l.campaign_id
            WHERE c.owner_id = u.id AND l.created_at >= ?) leads_7d, (SELECT COUNT(*) FROM campaigns c WHERE c.owner_id = u.id
            AND c.status IN ('programada', 'en curso')) active FROM users u ORDER BY leads_7d DESC, u.username LIMIT 8""", (seven,))
    return out
