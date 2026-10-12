"""Usuarios, contraseñas (PBKDF2) y sesiones por cookie."""
import hashlib
import hmac
import os
import secrets
import time
from collections import defaultdict

from fastapi import HTTPException, Request

from . import db

COOKIE = "lh_session"
SESSION_DAYS = 14
_fails: dict[str, list[float]] = defaultdict(list)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL COLLATE NOCASE,
    pw_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires REAL NOT NULL
);
"""


def hash_pw(pw: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 310_000).hex()
    return f"pbkdf2${salt}${h}"


def check_pw(pw: str, stored: str) -> bool:
    try:
        _, salt, h = stored.split("$")
    except ValueError:
        return False
    calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 310_000).hex()
    return hmac.compare_digest(calc, h)


def init():
    with db.connect() as c:
        c.executescript(SCHEMA)
    if db.one("SELECT COUNT(*) n FROM users")["n"] == 0:
        user = os.getenv("ADMIN_USER", "admin")
        pw = os.getenv("ADMIN_PASSWORD")
        if not pw:
            pw = secrets.token_urlsafe(12)
            print(f"\n*** Usuario inicial: {user}  contraseña: {pw}  (cámbiala al entrar) ***\n",
                  flush=True)
        create_user(user, pw, admin=True)


def create_user(username: str, pw: str, admin: bool = False):
    username = username.strip()
    if len(username) < 3 or len(pw) < 8:
        raise HTTPException(400, "Usuario mín. 3 caracteres y contraseña mín. 8.")
    if db.one("SELECT id FROM users WHERE username = ?", (username,)):
        raise HTTPException(400, "Ese usuario ya existe.")
    return db.execute("INSERT INTO users(username, pw_hash, is_admin) VALUES(?,?,?)",
                      (username, hash_pw(pw), int(admin)))


def _ip(req: Request) -> str:
    fwd = req.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (req.client.host if req.client else "?")


def login(req: Request, username: str, pw: str) -> str:
    ip, now = _ip(req), time.time()
    _fails[ip] = [t for t in _fails[ip] if now - t < 600]
    if len(_fails[ip]) >= 5:
        raise HTTPException(429, "Demasiados intentos. Espera 10 minutos.")
    u = db.one("SELECT * FROM users WHERE username = ?", (username.strip(),))
    if not u or not check_pw(pw, u["pw_hash"]):
        _fails[ip].append(now)
        raise HTTPException(401, "Usuario o contraseña incorrectos.")
    _fails.pop(ip, None)
    token = secrets.token_urlsafe(32)
    db.execute("DELETE FROM sessions WHERE expires < ?", (now,))
    db.execute("INSERT INTO sessions(token, user_id, expires) VALUES(?,?,?)",
               (token, u["id"], now + SESSION_DAYS * 86400))
    return token


def logout(token: str):
    db.execute("DELETE FROM sessions WHERE token = ?", (token,))


def current_user(req: Request):
    token = req.cookies.get(COOKIE)
    if not token:
        return None
    return db.one("""SELECT u.id, u.username, u.is_admin FROM sessions s
                     JOIN users u ON u.id = s.user_id
                     WHERE s.token = ? AND s.expires > ?""", (token, time.time()))


def require_admin(req: Request):
    u = getattr(req.state, "user", None)
    if not u or not u["is_admin"]:
        raise HTTPException(403, "Solo un administrador puede hacer esto.")
    return u
