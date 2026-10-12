"""Persistencia en SQLite: campañas, leads y ajustes."""
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "leads.db"
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    client TEXT,
    keywords TEXT NOT NULL,          -- JSON list
    locations TEXT NOT NULL,         -- JSON list
    sources TEXT NOT NULL,           -- JSON list
    options TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pendiente',
    progress REAL NOT NULL DEFAULT 0,
    log TEXT NOT NULL DEFAULT '[]',
    api_calls INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    dedupe_key TEXT NOT NULL,
    source TEXT NOT NULL,
    name TEXT,
    category TEXT,
    address TEXT,
    city TEXT,
    phone TEXT,
    whatsapp TEXT,
    emails TEXT DEFAULT '[]',        -- JSON list
    website TEXT,
    instagram TEXT,
    facebook TEXT,
    tiktok TEXT,
    linkedin TEXT,
    rating REAL,
    reviews INTEGER,
    maps_url TEXT,
    lat REAL, lng REAL,
    business_status TEXT,
    ig_followers INTEGER,
    ig_bio TEXT,
    extra TEXT DEFAULT '{}',         -- JSON (datos RUES, etc.)
    score INTEGER DEFAULT 0,
    status TEXT DEFAULT 'nuevo',     -- nuevo | contactado | interesado | descartado
    notes TEXT DEFAULT '',
    enriched INTEGER DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(campaign_id, dedupe_key)
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    grouping_note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS company_contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    identity_key TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT '',
    linkedin TEXT NOT NULL DEFAULT '',
    emails TEXT NOT NULL DEFAULT '[]',
    phones TEXT NOT NULL DEFAULT '[]',
    evidence_records TEXT NOT NULL DEFAULT '[]',
    relationship TEXT NOT NULL DEFAULT 'pending',
    review_data TEXT NOT NULL DEFAULT '{}',
    UNIQUE(company_id, identity_key)
);
"""

JSON_FIELDS = {"keywords", "locations", "sources", "options", "log", "emails", "extra"}
JSON_FIELDS |= {"provenance", "web_audit", "phones", "linkedin_data", "instagram_data", "evidence_records", "review_data"}
DICT_FIELDS = {"options", "extra", "provenance", "web_audit", "linkedin_data", "instagram_data", "review_data"}


@contextmanager
def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init():
    with _lock, connect() as c:
        c.executescript(SCHEMA)
        columns = {r[1] for r in c.execute("PRAGMA table_info(leads)")}
        for name, default in (("provenance", "{}"), ("web_audit", "{}"), ("phones", "[]"), ("linkedin_data", "{}"), ("instagram_data", "{}")):
            if name not in columns:
                c.execute(f"ALTER TABLE leads ADD COLUMN {name} TEXT NOT NULL DEFAULT '{default}'")
        if "company_id" not in columns:
            c.execute("ALTER TABLE leads ADD COLUMN company_id INTEGER REFERENCES companies(id)")
        if "company_locked" not in columns:
            c.execute("ALTER TABLE leads ADD COLUMN company_locked INTEGER NOT NULL DEFAULT 0")
        c.execute("CREATE INDEX IF NOT EXISTS leads_company ON leads(company_id)")


def _row(r):
    if r is None:
        return None
    d = dict(r)
    for k in JSON_FIELDS & d.keys():
        try:
            d[k] = json.loads(d[k]) if d[k] else ({} if k in DICT_FIELDS else [])
        except (TypeError, ValueError):
            pass
    return d


def query(sql, params=()):
    with _lock, connect() as c:
        return [_row(r) for r in c.execute(sql, params).fetchall()]


def one(sql, params=()):
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql, params=()):
    with _lock, connect() as c:
        cur = c.execute(sql, params)
        return cur.lastrowid


def _enc(v):
    return json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v


def insert(table, data):
    cols = ", ".join(data)
    qs = ", ".join("?" for _ in data)
    return execute(f"INSERT OR IGNORE INTO {table} ({cols}) VALUES ({qs})",
                   tuple(_enc(v) for v in data.values()))


def update(table, id_, data):
    if not data:
        return
    sets = ", ".join(f"{k} = ?" for k in data)
    execute(f"UPDATE {table} SET {sets} WHERE id = ?",
            tuple(_enc(v) for v in data.values()) + (id_,))


# ---- ajustes (API keys) ----
def get_setting(key, default=""):
    r = one("SELECT value FROM settings WHERE key = ?", (key,))
    return r["value"] if r and r["value"] is not None else default


def set_setting(key, value):
    execute("INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
