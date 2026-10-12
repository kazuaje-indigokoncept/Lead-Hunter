"""Arranque local con diagnóstico, .env y apertura tras comprobar el servidor."""
import argparse
from contextlib import closing
import importlib.util
import os
from pathlib import Path
import socket
import sqlite3
import sys
import threading
import time
import traceback
from urllib.request import urlopen
import webbrowser

ROOT = Path(__file__).resolve().parent
MODULES = ("fastapi", "pydantic", "uvicorn", "httpx", "openpyxl", "playwright")


def load_env(path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key.replace("_", "").isalnum():
            os.environ.setdefault(key, value)


def port_available(port):
    with socket.socket() as sock:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def diagnose(port):
    lines = [f"Python: {sys.version.split()[0]}", f"Ejecutable: {sys.executable}", f"Proyecto: {ROOT}"]
    missing = [name for name in MODULES if importlib.util.find_spec(name) is None]
    lines.append("Dependencias: " + ("faltan " + ", ".join(missing) if missing else "disponibles"))
    free_port = port_available(port)
    lines.append(f"Puerto {port}: " + ("disponible" if free_port else "ocupado; cierra la otra instancia o elige otro puerto"))
    lines.append("Archivo .env: " + ("presente (valores ocultos)" if (ROOT / ".env").exists() else "no existe; se usara la configuracion inicial"))
    database = ROOT / "data/leads.db"
    if database.exists():
        try:
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as conn:
                integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
            lines.append("Base de datos: " + integrity)
        except sqlite3.Error as exc:
            lines.append("Base de datos: " + str(exc))
    else:
        lines.append("Base de datos: se creara al iniciar")
    report = "\n".join(lines)
    print(report, flush=True)
    folder = ROOT / "logs"
    folder.mkdir(exist_ok=True)
    (folder / "diagnostico.txt").write_text(report + "\n", encoding="utf-8")
    print("Diagnostico guardado en logs/diagnostico.txt; no incluye contrasenas ni claves.", flush=True)
    return 1 if missing or not free_port or sys.version_info < (3, 10) else 0


def open_when_ready(url, stopped):
    deadline = time.monotonic() + 120
    while not stopped.is_set() and time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=1) as response:
                if response.status == 200 and b"LeadHunter" in response.read(1024):
                    webbrowser.open(url)
                    return
        except (OSError, ValueError):
            pass
        stopped.wait(0.4)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("Puerto invalido")
    os.chdir(ROOT)
    stopped = threading.Event()
    try:
        load_env(ROOT / ".env")
        if args.check:
            return diagnose(args.port)
        missing = [name for name in MODULES if importlib.util.find_spec(name) is None]
        if missing:
            raise RuntimeError("Faltan dependencias: " + ", ".join(missing) + ". Ejecuta iniciar.bat sin -SinInstalar.")
        if not port_available(args.port):
            raise RuntimeError(f"El puerto {args.port} esta ocupado. Cierra la otra instancia de LeadHunter o usa iniciar.bat -Puerto 8001.")
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / ".browsers"))
        url = f"http://127.0.0.1:{args.port}"
        print(f"Iniciando LeadHunter en {url}. Espera el mensaje Application startup complete.", flush=True)
        if not args.no_browser:
            threading.Thread(target=open_when_ready, args=(url, stopped), daemon=True).start()
        import uvicorn
        uvicorn.run("app.main:app", host="127.0.0.1", port=args.port)
        return 0
    except KeyboardInterrupt:
        return 0
    except (Exception, SystemExit):
        folder = ROOT / "logs"
        folder.mkdir(exist_ok=True)
        error = traceback.format_exc()
        (folder / "arranque_error.txt").write_text(error, encoding="utf-8")
        print(error, file=sys.stderr, flush=True)
        print("El error se ha guardado en logs/arranque_error.txt.", file=sys.stderr, flush=True)
        return 1
    finally:
        stopped.set()


if __name__ == "__main__":
    raise SystemExit(main())
