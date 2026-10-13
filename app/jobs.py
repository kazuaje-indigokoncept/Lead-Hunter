"""Ejecución de campañas en segundo plano."""
import asyncio
import json
import random
import time
import traceback
import math
import unicodedata
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from . import db, intelligence, companies, master
from .enrich import instagram, instagram_public, linkedin, website, people
from .sources import maps_web, places, rues

RUNNING: dict[int, asyncio.Task] = {}
STOP_REQUESTED: set[int] = set()
BATCH_RUNNING: set[int] = set()     # campañas ejecutando una tanda programada

# Programación por franja horaria: las tandas se lanzan a horas aleatorias dentro de una
# ventana diaria y se detienen al llegar al cupo de perfiles del día.
WINDOW_STEP = 30              # minutos entre posiciones de la barra horaria
MIN_WINDOW = 60               # la franja dura al menos una hora
DEFAULT_WINDOW = (8 * 60, 18 * 60)
DEFAULT_DAILY = 100
PROFILE_STEP = 10
DEFAULT_GLOBAL_CAP = 300      # perfiles por día en todo el servidor (Ajustes)
MAX_GLOBAL_CAP = 5000
MAX_BATCH_ERRORS = 3          # errores seguidos antes de dejar la campaña en "error"
ERROR_WAIT = 60 * 60          # tras un error se espera al menos una hora
SCHEDULER_TICK = 30           # segundos entre revisiones del programador
DEFAULT_TZ = "America/Bogota"


def app_tz():
    try:
        return ZoneInfo(db.get_setting("timezone", DEFAULT_TZ) or DEFAULT_TZ)
    except Exception:  # noqa: BLE001 - zona inválida: se usa la predeterminada
        return ZoneInfo(DEFAULT_TZ)


def local_now(ts=None):
    return datetime.fromtimestamp(ts if ts is not None else time.time(), app_tz())


def global_cap() -> int:
    try:
        return max(PROFILE_STEP, min(int(db.get_setting("max_profiles_day", str(DEFAULT_GLOBAL_CAP))), MAX_GLOBAL_CAP))
    except ValueError:
        return DEFAULT_GLOBAL_CAP


def schedule_of(camp) -> dict:
    """Programación de la campaña con valores por defecto. enabled=False → todo de una vez."""
    sch = (camp.get("options") or {}).get("schedule") or {}
    return {"enabled": bool(sch.get("enabled")),
            "window_start": int(sch.get("window_start", DEFAULT_WINDOW[0])),
            "window_end": int(sch.get("window_end", DEFAULT_WINDOW[1])),
            "daily_profiles": int(sch.get("daily_profiles", DEFAULT_DAILY))}


def combos_of(camp) -> list[tuple[str, str]]:
    return [(k, l) for k in camp["keywords"] for l in camp["locations"]]


def per_search(camp) -> int:
    """Perfiles que puede aportar una búsqueda completa, sin superar el cupo diario."""
    opts, srcs = camp.get("options") or {}, camp.get("sources") or []
    if "google_maps" in srcs:
        size = int(opts.get("maps_max_results", 40)) if opts.get("maps_mode", "api") == "web" else int(opts.get("max_pages", 3)) * 20
    elif "rues" in srcs:
        size = int(opts.get("rues_limit", 100))
    else:
        size = 40
    return max(1, min(size, schedule_of(camp)["daily_profiles"]))


def used_today(camp, day=None) -> int:
    day = day or local_now().date().isoformat()
    return (camp.get("daily_used") or 0) if camp.get("daily_date") == day else 0


def server_used_today(day=None) -> int:
    day = day or local_now().date().isoformat()
    return db.one("SELECT COALESCE(SUM(daily_used), 0) n FROM campaigns WHERE daily_date = ?", (day,))["n"]


def quota_left(camp) -> int:
    """Perfiles que la campaña aún puede consultar hoy (cupo propio y tope del servidor)."""
    day = local_now().date().isoformat()
    own = schedule_of(camp)["daily_profiles"] - used_today(camp, day)
    return max(0, min(own, global_cap() - server_used_today(day)))


def window_bounds(day, sch, tz):
    base = datetime(day.year, day.month, day.day, tzinfo=tz)
    return base + timedelta(minutes=sch["window_start"]), base + timedelta(minutes=sch["window_end"])


def in_window(camp, ts=None) -> bool:
    now = local_now(ts)
    start, end = window_bounds(now.date(), schedule_of(camp), now.tzinfo)
    return start <= now < end


def plan_next(camp, ts=None) -> float:
    """Hora aleatoria de la siguiente tanda, dentro de la franja y repartiendo el cupo del día.

    Las tandas que faltan hoy se reparten en lo que queda de franja: la siguiente cae en un
    punto al azar del primer tramo, así cada día las horas son distintas.
    """
    now = local_now(ts)
    tz, sch = now.tzinfo, schedule_of(camp)
    per = per_search(camp)
    left_combos = max(1, len(combos_of(camp)) - (camp.get("batch_cursor") or 0))
    day = now.date()
    start, end = window_bounds(day, sch, tz)
    remaining = quota_left(camp)
    if now >= end - timedelta(minutes=5) or remaining < 1:
        day += timedelta(days=1)
        start, end = window_bounds(day, sch, tz)
        remaining, frm = sch["daily_profiles"], start
    else:
        frm = max(now, start)
    batches = max(1, min(math.ceil(remaining / per), left_combos))
    slot = (end - frm).total_seconds() / batches
    when = frm + timedelta(seconds=slot * random.uniform(0.1, 0.9))
    return max(when.timestamp(), (now + timedelta(minutes=1)).timestamp())


def score(lead: dict) -> int:
    s = 0
    emails = lead.get("emails") or []
    if isinstance(emails, str):
        emails = json.loads(emails or "[]")
    s += 25 if emails else 0
    s += 20 if lead.get("whatsapp") else 0
    s += 15 if lead.get("phone") else 0
    s += 10 if lead.get("website") else 0
    s += 10 if lead.get("instagram") or lead.get("facebook") else 0
    s += 10 if (lead.get("rating") or 0) >= 4 else 0
    s += 10 if (lead.get("reviews") or 0) >= 20 else 0
    return s


def apply_enrichment(lead, data, *, demo=False):
    upd = {}
    upd["emails"] = list(dict.fromkeys((lead.get("emails") or []) + (data.get("emails") or [])))
    upd["phones"] = list(dict.fromkeys((lead.get("phones") or []) + ([lead["phone"]] if lead.get("phone") else []) + (data.get("phones") or [])))
    for f in ("whatsapp", "instagram", "facebook", "tiktok", "linkedin"):
        if data.get(f) and not lead.get(f):
            upd[f] = data[f]
    if data.get("phone_web") and not lead.get("phone"):
        upd["phone"] = data["phone_web"]
    added = data.get("provenance") or {}
    if demo:
        added = intelligence.initial_provenance({**data, "phone": data.get("phone_web")}, "demo", demo=True)
        for field in ("ig_bio", "ig_followers"):
            if data.get(field) is not None:
                added[field] = [intelligence.evidence(data[field], "demo", lead.get("instagram", ""), verification="demo", confidence="baja")]
    upd["provenance"] = intelligence.merge_provenance(lead.get("provenance"), added)
    if "web_audit" in data:
        upd["web_audit"] = data["web_audit"]
        upd["enriched"] = int(data["web_audit"].get("status") == "ok" and not data["web_audit"].get("partial"))
    if "linkedin_data" in data:
        upd["linkedin_data"] = data["linkedin_data"]
    if "instagram_data" in data:
        upd["instagram_data"] = data["instagram_data"]
    for field in ("ig_bio", "ig_followers"):
        if data.get(field) is not None:
            upd[field] = data[field]
    if data.get("error"):
        upd["notes"] = (lead.get("notes") or "") + f"[web] {data['error']} "
    upd["score"] = score({**lead, **upd})
    db.update("leads", lead["id"], upd)
    updated = db.one("SELECT * FROM leads WHERE id = ?", (lead["id"],))
    cid = companies.attach(updated)
    if not demo:
        people.store(cid, data.get("people_candidates", []))
    final = db.one("SELECT * FROM leads WHERE id = ?", (lead["id"],))
    master.safe_sync(final)
    return db.one("SELECT * FROM leads WHERE id = ?", (lead["id"],))


async def enrich_lead(client, lead, demo=False, max_extra_pages=4):
    data = demo_web(lead) if demo else await website.enrich(client, lead["website"], max_extra_pages=max_extra_pages)
    return apply_enrichment(lead, data, demo=demo)


async def enrich_instagram(client, lead, *, mode="web", demo=False, token="", ig_id=""):
    if demo:
        data = {"ig_followers": 1250, "ig_bio": "Perfil de demostración", "instagram_data": {
            "status": "demo", "mode": mode, "url": lead["instagram"], "checked_at": intelligence.now()}}
    elif mode == "web":
        data = await instagram_public.lookup(client, lead["instagram"])
    else:
        user = website.ig_username(lead["instagram"])
        data = await instagram.lookup(client, ig_id, token, user)
        data["instagram_data"] = {"status": "api_error" if data.get("error") else "api_success", "mode": "api",
            "url": lead["instagram"], "checked_at": intelligence.now(), "note": data.get("error", "")}
        data["provenance"] = {field: [intelligence.evidence(data[field], "instagram_api", lead["instagram"])]
                              for field in ("ig_bio", "ig_followers") if data.get(field) is not None}
        # Los errores de Meta se registran en la revisión de IG, no como fallos de la web.
        data.pop("error", None)
    return apply_enrichment(lead, data, demo=demo)


class Runner:
    def __init__(self, cid: int, batch: tuple[int, int] | None = None, profile_limit: int | None = None):
        self.cid = cid
        self.camp = db.one("SELECT * FROM campaigns WHERE id = ?", (cid,))
        self.opts = self.camp["options"] or {}
        self.all_combos = combos_of(self.camp)
        # batch = (inicio, fin) sobre la lista de búsquedas; None = campaña completa.
        self.batch = batch
        self.combos = self.all_combos[batch[0]:batch[1]] if batch else self.all_combos
        self.touched: set[int] = set()   # leads guardados en esta ejecución
        self.profile_limit = profile_limit  # tope de perfiles de esta tanda (cupo diario)
        self.demo = db.get_setting("demo_mode", "1") == "1"
        self.gkey = db.get_setting("google_api_key")
        self.calls = self.camp.get("api_calls", 0)
        # Las tandas continúan el registro anterior en vez de borrarlo.
        self.log_lines = list(self.camp.get("log") or []) if batch else []
        self.maps_mode = self.opts.get("maps_mode", "api")

    def check_stop(self):
        if self.cid in STOP_REQUESTED:
            raise asyncio.CancelledError

    # ---------- utilidades ----------
    def log(self, msg):
        line = f"{local_now():%H:%M:%S} {msg}"
        self.log_lines.append(line)
        db.update("campaigns", self.cid, {"log": self.log_lines[-200:]})

    def progress(self, p, status="en curso"):
        if self.batch:
            # Progreso de la tanda llevado al total de la campaña.
            total = max(len(self.all_combos), 1)
            p = (self.batch[0] + p * (self.batch[1] - self.batch[0])) / total
        db.update("campaigns", self.cid, {"progress": round(min(p, 1), 3), "status": status,
                                          "api_calls": self.calls})

    def scope(self, sql: str) -> tuple[str, tuple]:
        """En una tanda, el enriquecimiento se limita a los leads encontrados en ella."""
        if not self.batch:
            return sql, ()
        ids = sorted(self.touched) or [0]
        return sql + f" AND id IN ({','.join('?' * len(ids))})", tuple(ids)

    def count_call(self):
        self.calls += 1

    def room(self) -> int | None:
        """Perfiles que aún caben en esta tanda (None = sin tope)."""
        return None if self.profile_limit is None else max(0, self.profile_limit - len(self.touched))

    def save(self, lead: dict, city: str):
        if self.room() == 0:
            return
        lead = {**lead, "campaign_id": self.cid}
        lead.setdefault("city", city)
        lead["score"] = score(lead)
        origin = "google_maps_" + self.maps_mode if lead.get("source") == "google_maps" else lead.get("source", "unknown")
        lead["provenance"] = intelligence.merge_provenance(lead.get("provenance"), intelligence.initial_provenance(lead, origin, self.demo))
        lead["phones"] = [lead["phone"]] if lead.get("phone") else []
        previous = db.one("SELECT * FROM leads WHERE campaign_id = ? AND dedupe_key = ?", (self.cid, lead["dedupe_key"]))
        if previous:
            upd = {k: v for k, v in lead.items() if k not in ("campaign_id", "dedupe_key", "provenance", "phones", "score") and v is not None}
            upd["provenance"] = intelligence.merge_provenance(previous.get("provenance"), lead["provenance"])
            upd["phones"] = list(dict.fromkeys((previous.get("phones") or []) + lead["phones"]))
            if lead.get("website") and lead["website"] != previous.get("website"):
                upd.update(enriched=0, web_audit={})
            upd["score"] = score({**previous, **upd})
            db.update("leads", previous["id"], upd)
        else:
            db.insert("leads", lead)
        saved = db.one("SELECT * FROM leads WHERE campaign_id=? AND dedupe_key=?", (self.cid, lead["dedupe_key"]))
        self.touched.add(saved["id"])
        companies.attach(saved)
        master.safe_sync(db.one("SELECT * FROM leads WHERE id = ?", (saved["id"],)))

    # ---------- fases ----------
    async def run(self):
        kws, locs, srcs = self.camp["keywords"], self.camp["locations"], self.camp["sources"]
        phases = [s for s in ("google_maps", "rues", "website", "linkedin", "instagram")
                  if s in srcs or (s == "website" and any(net in srcs for net in ("linkedin", "instagram")))]
        if self.batch:
            first, last = self.batch
            self.log(f"Tanda: búsqueda {first + 1} de {len(self.all_combos)} · "
                     + ", ".join(f"'{k} en {l}'" for k, l in self.combos)
                     + (f" · hasta {self.profile_limit} perfiles" if self.profile_limit is not None else ""))
        self.log(f"Inicio. Modo {'DEMO' if self.demo else 'REAL'}. Fuentes: {', '.join(phases)}")
        if not self.demo and "google_maps" in srcs and self.maps_mode == "api" and not self.gkey:
            raise RuntimeError("Falta la API key de Google (Ajustes).")
        n = len(phases) or 1
        async with httpx.AsyncClient(headers={"User-Agent": website.UA},
                                     follow_redirects=True) as client:
            for i, ph in enumerate(phases):
                self.check_stop()
                base, span = i / n, 1 / n
                await getattr(self, f"phase_{ph}")(client, kws, locs, base, span)
        total = db.one("SELECT COUNT(*) n FROM leads WHERE campaign_id = ?", (self.cid,))["n"]
        self.check_stop()
        if self.batch:
            self.log(f"Tanda terminada: {len(self.touched)} leads en esta tanda, {total} en total.")
            self.progress(1, "en curso")
            return
        self.log(f"Terminado: {total} leads, {self.calls} llamadas a Google.")
        self.progress(1, "terminada")

    async def phase_google_maps(self, client, kws, locs, base, span):
        if not self.demo and self.maps_mode == "web":
            await self.phase_maps_web(kws, locs, base, span)
            return
        combos = self.combos
        pages = int(self.opts.get("max_pages", 3))
        min_rating = self.opts.get("min_rating") or None
        for j, (k, l) in enumerate(combos):
            q = f"{k} en {l}"
            if self.demo:
                res = demo_places(k, l)
                await asyncio.sleep(0.3)
            else:
                try:
                    res = await places.search(client, self.gkey, q, max_pages=pages,
                                              region=self.opts.get("region", "CO"),
                                              min_rating=min_rating, on_call=self.count_call)
                except places.PlacesError as e:
                    self.log(f"Error en '{q}': {e}")
                    if "403" in str(e) or "400" in str(e):
                        raise
                    continue
            if self.opts.get("only_operational", True):
                res = [r for r in res if r.get("business_status") in (None, "OPERATIONAL")]
            if self.room() is not None:
                res = res[:self.room()]
            for r in res:
                self.save(r, l)
            self.log(f"Google Maps · '{q}': {len(res)} resultados")
            self.progress(base + span * (j + 1) / len(combos))

    async def phase_maps_web(self, kws, locs, base, span):
        combos = self.combos
        limit = int(self.opts.get("maps_max_results", 40))
        if self.room() is not None:
            limit = min(limit, self.room())   # no visitar más fichas que el cupo de la tanda
        self.log(f"Google Maps sin API · hasta {limit} fichas por consulta. No consume llamadas a Places.")
        async with maps_web.MapsBrowser(log=self.log, check_stop=self.check_stop) as browser:
            for j, (k, l) in enumerate(combos):
                self.check_stop()
                count = 0
                if limit < 1:
                    break
                async for lead in browser.search(
                    f"{k} en {l}", max_results=limit,
                    min_rating=self.opts.get("min_rating") or None,
                    delay=float(self.opts.get("maps_delay", 2)),
                ):
                    self.check_stop()
                    if self.opts.get("only_operational", True) and lead.get("business_status") == "CLOSED_PERMANENTLY":
                        continue
                    self.save(lead, l)
                    count += 1
                    self.progress(base + span * (j + min(count / limit, 0.99)) / len(combos))
                self.check_stop()
                self.log(f"Google Maps sin API · '{k} en {l}': {count} fichas obtenidas")
                self.progress(base + span * (j + 1) / len(combos))

    async def phase_rues(self, client, kws, locs, base, span):
        combos = self.combos
        since = self.opts.get("rues_since", "")
        ciiu = self.opts.get("rues_ciiu", "")
        cross = self.opts.get("rues_cross_maps", False)
        if cross and self.maps_mode == "web":
            self.log("Cruce RUES omitido: requiere Places API. La búsqueda de Maps sin API se ejecuta por separado.")
            cross = False
        for j, (k, l) in enumerate(combos):
            try:
                res = demo_rues(k, l) if self.demo else await rues.search(
                    client, keyword=k if not ciiu else "", camara=l, ciiu=ciiu, since=since,
                    limit=min(int(self.opts.get("rues_limit", 100)), self.room() if self.room() is not None else 10**6),
                    app_token=db.get_setting("socrata_token"))
            except httpx.HTTPError as e:
                self.log(f"RUES error ({l}): {e}")
                continue
            for r in res:
                if cross and (self.gkey or self.demo):
                    match = await self._match_maps(client, r["name"], l)
                    if match:
                        r = {**r, **{k2: v for k2, v in match.items()
                                     if k2 not in ("dedupe_key", "source", "name") and v}}
                        r["source"] = "rues+maps"
                self.save(r, l)
            self.log(f"RUES · '{k}' en cámara '{l}': {len(res)} establecimientos activos")
            self.progress(base + span * (j + 1) / len(combos))

    async def _match_maps(self, client, name, city):
        if self.demo:
            return demo_places(name, city)[0] if random.random() < 0.6 else None
        try:
            res = await places.search(client, self.gkey, f"{name} {city}", max_pages=1,
                                      on_call=self.count_call)
        except places.PlacesError:
            return None
        return res[0] if res else None

    async def phase_website(self, client, kws, locs, base, span):
        sql, extra = self.scope("SELECT * FROM leads WHERE campaign_id = ? AND website IS NOT NULL "
                                "AND website != '' AND (enriched = 0 OR web_audit = '{}')")
        leads = db.query(sql, (self.cid,) + extra)
        self.log(f"Visitando {len(leads)} sitios web…")
        sem = asyncio.Semaphore(int(self.opts.get("concurrency", 8)))
        done = 0

        async def work(lead):
            nonlocal done
            async with sem:
                self.check_stop()
                await enrich_lead(client, lead, self.demo, int(self.opts.get("web_max_pages", 4)))
                if self.demo:
                    await asyncio.sleep(0.05)
            done += 1
            if done % 5 == 0 or done == len(leads):
                self.progress(base + span * done / max(len(leads), 1))

        await asyncio.gather(*(work(l) for l in leads))
        self.progress(base + span)

    async def phase_linkedin(self, client, kws, locs, base, span):
        sql, extra = self.scope("SELECT * FROM leads WHERE campaign_id = ? AND linkedin IS NOT NULL AND linkedin != ''")
        leads = db.query(sql, (self.cid,) + extra)
        self.log(f"LinkedIn: {len(leads)} enlaces encontrados; consulta pública de empresas con comprobación de robots.txt.")
        for j, lead in enumerate(leads):
            self.check_stop()
            data = {"linkedin_data": {"status": "demo", "checked_at": intelligence.now(), "url": lead["linkedin"]}} if self.demo else await linkedin.lookup(client, lead["linkedin"])
            apply_enrichment(lead, data, demo=self.demo)
            self.log(f"LinkedIn · {lead['name']}: {data['linkedin_data']['status']}")
            self.progress(base + span * 0.5 * (j + 1) / max(len(leads), 1))
            if data["linkedin_data"].get("status_code") == 429:
                self.log("LinkedIn: fase detenida por límite de acceso; se conservan los datos anteriores.")
                self.progress(base + span)
                return
            await asyncio.sleep(0.05 if self.demo else 2)
        sql, extra = self.scope("SELECT DISTINCT company_id FROM leads WHERE campaign_id=? AND company_id IS NOT NULL")
        company_ids = [r["company_id"] for r in db.query(sql, (self.cid,) + extra)]
        self.log(f"Buscando responsables asociados a {len(company_ids)} empresas; hasta 15 perfiles públicos por empresa.")
        for index, cid in enumerate(company_ids):
            self.check_stop()
            result = await linkedin.review_company_people(client, cid, demo=self.demo, check_stop=self.check_stop, discover=False)
            self.log(f"Responsables · empresa #{cid}: {result['status']}, {result['reviewed']} perfiles revisados")
            self.progress(base + span * (0.5 + 0.5 * (index + 1) / max(len(company_ids), 1)))
            if result["status"] == "access_restricted":
                self.log("LinkedIn: límite de acceso; fin de la revisión de responsables en esta campaña.")
                break
        self.progress(base + span)

    async def phase_instagram(self, client, kws, locs, base, span):
        mode = self.opts.get("instagram_mode", "api")
        token = db.get_setting("meta_token")
        ig_id = db.get_setting("ig_user_id")
        if mode == "api" and not self.demo and not (token and ig_id):
            self.log("Instagram omitido: faltan token de Meta / ID de cuenta IG en Ajustes.")
            self.progress(base + span)
            return
        sql, extra = self.scope("SELECT * FROM leads WHERE campaign_id = ? AND instagram IS NOT NULL "
                                "AND instagram != ''")
        leads = db.query(sql, (self.cid,) + extra)
        self.log(f"Instagram {'público sin API' if mode == 'web' else 'API de Meta'}: {len(leads)} perfiles encontrados en las webs o añadidos manualmente.")
        for j, lead in enumerate(leads):
            self.check_stop()
            updated = await enrich_instagram(client, lead, mode=mode, demo=self.demo, token=token, ig_id=ig_id)
            audit = updated["instagram_data"]
            self.log(f"Instagram · {lead['name']}: {audit['status']}")
            self.progress(base + span * (j + 1) / max(len(leads), 1))
            if audit.get("status_code") == 429 or (mode == "api" and "limit" in audit.get("note", "").lower()):
                self.log("Instagram: pausa de esta fase por límite de acceso. Puedes volver a revisar más tarde.")
                break
            await asyncio.sleep(0.05 if self.demo else 2)
        self.progress(base + span)


async def run_campaign(cid: int):
    r = Runner(cid)
    try:
        await r.run()
    except asyncio.CancelledError:
        r.log("Detenida por el usuario.")
        row = db.one("SELECT progress FROM campaigns WHERE id=?", (cid,))
        if row:
            r.progress(row["progress"], "detenida")
    except Exception as e:  # noqa: BLE001
        r.log(f"ERROR: {e}")
        traceback.print_exc()
        r.progress(0, "error")
    finally:
        RUNNING.pop(cid, None)
        STOP_REQUESTED.discard(cid)


def _record_run(camp, entry):
    runs = (camp.get("runs") or []) + [{"at": time.time(), **entry}]
    return runs[-60:]


def _when(ts):
    t = local_now(ts)
    return f"{t:%H:%M}" if t.date() == local_now().date() else f"{t:%d/%m %H:%M}"


async def run_batch(cid: int, manual: bool = False):
    """Ejecuta la siguiente búsqueda dentro del cupo del día y programa la siguiente tanda."""
    camp = db.one("SELECT * FROM campaigns WHERE id = ?", (cid,))
    total = len(combos_of(camp))
    cursor = min(camp.get("batch_cursor") or 0, total)
    day = local_now().date().isoformat()
    used = used_today(camp, day)
    room = quota_left(camp)

    def reschedule(msg):
        fresh = db.one("SELECT * FROM campaigns WHERE id = ?", (cid,))
        nxt = plan_next(fresh)
        lines = (fresh.get("log") or []) + [f"{local_now():%H:%M:%S} {msg} Próxima tanda: {_when(nxt)}."]
        db.update("campaigns", cid, {"status": "programada", "next_run_at": nxt, "log": lines[-200:]})

    try:
        if room < 1:
            reschedule("Cupo de perfiles de hoy alcanzado.")
            return
        if not manual and not in_window(camp):
            reschedule("Fuera de la franja horaria configurada.")
            return
        per = per_search(camp)
        limit = min(per, room)
        r = Runner(cid, (cursor, cursor + 1), profile_limit=limit)
        try:
            await r.run()
        except asyncio.CancelledError:
            r.log("Programación pausada por el usuario. Pulsa Reanudar para continuar.")
            db.update("campaigns", cid, {"status": "pausada", "next_run_at": None,
                                         "daily_date": day, "daily_used": used + len(r.touched)})
            return
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            errors = (camp.get("batch_errors") or 0) + 1
            fresh = db.one("SELECT * FROM campaigns WHERE id = ?", (cid,))
            upd = {"batch_errors": errors, "daily_date": day, "daily_used": used + len(r.touched),
                   "runs": _record_run(fresh, {"index": cursor, "profiles": len(r.touched), "status": "error", "manual": manual})}
            if errors >= MAX_BATCH_ERRORS:
                r.log(f"ERROR: {e}. {errors} tandas seguidas con error: programación detenida.")
                db.update("campaigns", cid, {**upd, "status": "error", "next_run_at": None})
            else:
                # Ante un bloqueo o fallo se espera al menos una hora antes de repetir la búsqueda.
                nxt = max(plan_next({**fresh, **upd}), time.time() + ERROR_WAIT)
                r.log(f"ERROR: {e}. Se repetirá esta búsqueda hacia las {_when(nxt)}.")
                db.update("campaigns", cid, {**upd, "status": "programada", "next_run_at": nxt})
            return
        got = len(r.touched)
        # Si el cupo cortó la búsqueda, mañana se repite completa (los repetidos se unifican).
        capped = limit < per and got >= limit
        new_cursor = cursor if capped else cursor + 1
        fresh = db.one("SELECT * FROM campaigns WHERE id = ?", (cid,))
        upd = {"batch_cursor": new_cursor, "batch_errors": 0, "daily_date": day, "daily_used": used + got,
               "runs": _record_run(fresh, {"index": cursor, "profiles": got, "status": "parcial" if capped else "ok", "manual": manual})}
        if new_cursor >= total:
            r.log(f"Campaña completada: {total} búsquedas procesadas en tandas.")
            db.update("campaigns", cid, {**upd, "status": "terminada", "progress": 1, "next_run_at": None})
        else:
            nxt = plan_next({**fresh, **upd})
            r.log(f"{got} perfiles en esta tanda · hoy {used + got} de {schedule_of(camp)['daily_profiles']}. "
                  + ("Cupo alcanzado: esta búsqueda se completará en el próximo día. " if capped else "")
                  + f"Próxima tanda: {_when(nxt)} ({new_cursor} de {total} búsquedas hechas).")
            db.update("campaigns", cid, {**upd, "status": "programada", "next_run_at": nxt})
    finally:
        RUNNING.pop(cid, None)
        BATCH_RUNNING.discard(cid)
        STOP_REQUESTED.discard(cid)


def _launch_batch(cid: int, manual: bool = False):
    BATCH_RUNNING.add(cid)
    RUNNING[cid] = asyncio.create_task(run_batch(cid, manual))


def _restart_if_done(camp):
    if (camp.get("batch_cursor") or 0) >= len(combos_of(camp)):
        # Campaña ya completada: se vuelve a recorrer desde la primera búsqueda.
        db.update("campaigns", camp["id"], {"batch_cursor": 0, "progress": 0})


def start(cid: int):
    """Ejecuta la campaña completa o, si está programada, su siguiente tanda ahora mismo."""
    if cid in RUNNING:
        return False
    camp = db.one("SELECT * FROM campaigns WHERE id = ?", (cid,))
    if camp and schedule_of(camp)["enabled"]:
        _restart_if_done(camp)
        db.update("campaigns", cid, {"status": "en curso", "batch_errors": 0})
        _launch_batch(cid, manual=True)
        return True
    RUNNING[cid] = asyncio.create_task(run_campaign(cid))
    return True


def schedule(cid: int):
    """Activa o reanuda la programación: la siguiente tanda cae al azar dentro de la franja."""
    if cid in RUNNING:
        return False
    camp = db.one("SELECT * FROM campaigns WHERE id = ?", (cid,))
    if not camp or not schedule_of(camp)["enabled"]:
        return False
    _restart_if_done(camp)
    camp = db.one("SELECT * FROM campaigns WHERE id = ?", (cid,))
    nxt = plan_next(camp)
    lines = (camp.get("log") or []) + [f"{local_now():%H:%M:%S} Programación activa. Primera tanda: {_when(nxt)}."]
    db.update("campaigns", cid, {"status": "programada", "next_run_at": nxt, "batch_errors": 0, "log": lines[-200:]})
    return True


def stop(cid: int):
    t = RUNNING.get(cid)
    if t:
        c = db.one("SELECT options FROM campaigns WHERE id = ?", (cid,))
        if c and c["options"].get("maps_mode") == "web":
            # Playwright no admite cancelar una operación a mitad: parar entre operaciones.
            STOP_REQUESTED.add(cid)
        else:
            t.cancel()
        return True
    camp = db.one("SELECT status FROM campaigns WHERE id = ?", (cid,))
    if camp and camp["status"] == "programada":
        # Esperando la siguiente tanda: se pausa sin tocar lo ya encontrado.
        db.update("campaigns", cid, {"status": "pausada", "next_run_at": None})
        return True
    return False


def scheduler_tick():
    """Lanza la tanda vencida más antigua. Solo una tanda programada a la vez."""
    if BATCH_RUNNING:
        return None
    due = db.one("SELECT id FROM campaigns WHERE status = 'programada' AND next_run_at IS NOT NULL "
                 "AND next_run_at <= ? ORDER BY next_run_at LIMIT 1", (time.time(),))
    if not due or due["id"] in RUNNING:
        return None
    db.update("campaigns", due["id"], {"status": "en curso"})
    _launch_batch(due["id"])
    return due["id"]


async def scheduler_loop():
    while True:
        try:
            scheduler_tick()
        except Exception:  # noqa: BLE001 - el programador no debe detenerse por un fallo puntual
            traceback.print_exc()
        await asyncio.sleep(SCHEDULER_TICK)


def recover():
    """Tras un reinicio, ninguna campaña sigue realmente en curso."""
    for c in db.query("SELECT * FROM campaigns WHERE status = 'en curso'"):
        log = (c["log"] or []) + [f"{local_now():%H:%M:%S} Interrumpida por un reinicio del servidor."]
        if schedule_of(c)["enabled"]:
            # La búsqueda interrumpida se repite en la franja horaria.
            db.update("campaigns", c["id"], {"status": "programada", "next_run_at": plan_next(c), "log": log[-200:]})
        else:
            db.update("campaigns", c["id"], {"status": "detenida", "log": log[-200:]})


# ---------------- datos de demostración ----------------
_NAMES = ["Central", "del Parque", "Express", "Premium", "La 70", "El Poblado", "Norte",
          "Familiar", "Integral", "Sonrisa", "Vital", "Estudio", "Plus", "Andino", "Real"]
_STREETS = ["Cra 43A # 1-50", "Cl 10 # 38-20", "Av 80 # 33-12", "Cl 33 # 65-40",
            "Cra 70 # 44-10", "Cl 85 # 11-53", "Av 19 # 104-37", "Cra 7 # 72-64"]


def demo_places(kw, loc):
    rnd = random.Random(f"{kw}|{loc}")
    out = []
    for i in range(rnd.randint(12, 24)):
        name = f"{kw.title()} {rnd.choice(_NAMES)} {i + 1}"
        plain = unicodedata.normalize("NFKD", name.lower()).encode("ascii", "ignore").decode()
        slug = "".join(c for c in plain if c.isalnum())[:18]
        has_web = rnd.random() < 0.65
        cel = f"3{rnd.randint(0, 2)}{rnd.randint(0, 9)}{rnd.randint(1000000, 9999999)}"
        is_cel = rnd.random() < 0.6
        phone = f"{cel[:3]} {cel[3:6]} {cel[6:]}" if is_cel else f"(604) {rnd.randint(2000000, 4999999)}"
        out.append({
            "dedupe_key": f"gmaps:demo:{slug}:{loc}",
            "source": "google_maps", "name": name, "category": kw.title(),
            "address": f"{rnd.choice(_STREETS)}, {loc}",
            "phone": phone, "whatsapp": f"https://wa.me/57{cel}" if is_cel else None,
            "website": f"https://{slug}.com.co" if has_web else None,
            "rating": round(rnd.uniform(3.4, 5.0), 1), "reviews": rnd.randint(0, 600),
            "maps_url": f"https://maps.google.com/?q={name.replace(' ', '+')}",
            "lat": 6.2 + rnd.random() / 10, "lng": -75.6 + rnd.random() / 10,
            "business_status": "OPERATIONAL",
        })
    return out


def demo_rues(kw, loc):
    rnd = random.Random(f"rues|{kw}|{loc}")
    return [{
        "dedupe_key": f"rues:demo:{kw}:{loc}:{i}", "source": "rues",
        "name": f"{kw.title()} {rnd.choice(_NAMES)} SAS", "category": "CIIU 5611",
        "city": loc, "extra": {"fecha_matricula": f"2026-0{rnd.randint(1, 8)}-{rnd.randint(10, 28)}",
                               "camara": loc.upper(), "matricula": str(rnd.randint(100000, 999999))},
    } for i in range(rnd.randint(4, 9))]


def demo_web(lead):
    rnd = random.Random(lead["website"])
    dom = lead["website"].split("//")[-1]
    slug = dom.split(".")[0]
    out = {"web_audit": {"status": "ok", "checked_at": intelligence.now(), "resolved_url": lead["website"],
        "pages": [lead["website"]], "https": True, "has_title": True, "has_viewport": rnd.random() > 0.3,
        "has_meta_description": rnd.random() > 0.5, "has_contact_form": rnd.random() > 0.5, "partial": False}}
    if rnd.random() < 0.7:
        out["emails"] = [f"{rnd.choice(['info', 'contacto', 'ventas', 'hola'])}@{dom}"]
    if rnd.random() < 0.6:
        out["instagram"] = f"https://instagram.com/{slug}"
    if rnd.random() < 0.5:
        out["facebook"] = f"https://facebook.com/{slug}"
    if rnd.random() < 0.4:
        out["linkedin"] = f"https://www.linkedin.com/company/{slug}"
    if rnd.random() < 0.3 and not lead.get("whatsapp"):
        out["whatsapp"] = f"https://wa.me/573{rnd.randint(100000000, 299999999)}"
    return out
