#!/usr/bin/env python3
"""Start Dispatch v2 on any OS: the API (port 8080) and the console (port 8001).

    python run.py              start everything and open the console in your browser
    python run.py --fresh      wipe the database and load a fresh demo day first
    python run.py --no-open    don't open a browser tab
    python run.py --no-docker  skip Docker: SQLite + straight-line routing

What it does:
1. Starts the Docker services (Postgres, Redis, and OSRM if its data is built) when Docker is
   installed. Uses Postgres if it answers, otherwise a local SQLite file. Routing falls back to
   straight-line distance when OSRM isn't running, so everything works without Docker.
2. Loads the Chennai demo (depot, 3 vans, 10 orders) into an empty database.
3. Starts the v2 API and serves web/console.html.
4. No Gemini key? The console asks for one, checks it with Google, saves it to .env and
   restarts the API. Without a key, reported problems wait in "Needs you" for a person.

Uses the project's .venv automatically. Ctrl+C stops everything. Standard library only.
Env overrides: API_PORT, UI_PORT, DATABASE_URL.
"""

import argparse
import functools
import http.server
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(ROOT, "web")
ENV_FILE = os.path.join(ROOT, ".env")
COMPOSE = os.path.join(ROOT, "infra", "docker-compose.yml")
OSRM_DATA = os.path.join(ROOT, "infra", "osrm-data", "region.osrm")
API_PORT = int(os.environ.get("API_PORT", 8080))
UI_PORT = int(os.environ.get("UI_PORT", 8001))
PG_URL = "postgresql+psycopg://dispatch:dispatch@127.0.0.1:5432/dispatch"
UI_ORIGINS = [f"http://localhost:{UI_PORT}", f"http://127.0.0.1:{UI_PORT}"]
KEY_RE = re.compile(r"^[A-Za-z0-9._\-]{20,200}$")

restart_api = threading.Event()


# ---------------------------------------------------------------- environment
def venv_python() -> str | None:
    sub = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
    path = os.path.join(ROOT, ".venv", *sub)
    return path if os.path.exists(path) else None


def docker_exe() -> str | None:
    found = shutil.which("docker")
    if found:
        return found
    if os.name == "nt":                     # Docker Desktop per-user install isn't always on PATH
        p = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "DockerDesktop", "resources", "bin", "docker.exe")
        return p if os.path.exists(p) else None
    return None


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    with socket.socket() as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def wait_port(port: int, seconds: float) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if port_open(port):
            return True
        time.sleep(0.5)
    return False


def start_docker(want_osrm: bool) -> None:
    exe = docker_exe()
    if not exe:
        print("Docker not found: using SQLite and straight-line routing.")
        return
    cmd = [exe, "compose", "-f", COMPOSE] + (["--profile", "osrm"] if want_osrm else []) + ["up", "-d"]
    print("Starting Docker services (Postgres, Redis" + (", OSRM" if want_osrm else "") + ")...", flush=True)
    try:
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            print(f"  Docker didn't start the services ({(r.stderr or r.stdout).strip().splitlines()[-1:]}); "
                  "continuing without them.")
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"  Docker unavailable ({e}); continuing without it.")


# ---------------------------------------------------------------- key handling
def env_has_key() -> bool:
    if os.environ.get("GOOGLE_API_KEY"):
        return True
    try:
        with open(ENV_FILE, encoding="utf-8-sig") as f:
            return any(re.match(r"\s*GOOGLE_API_KEY\s*=\s*\S+", line) for line in f)
    except OSError:
        return False


def check_key(key: str) -> tuple[bool, str]:
    url = f"https://generativelanguage.googleapis.com/v1beta/models?pageSize=1&key={key}"
    try:
        urllib.request.urlopen(url, timeout=10).close()
        return True, "ok"
    except urllib.error.HTTPError as e:
        if e.code in (400, 401, 403):
            return False, "Google rejected this key. Copy it again from AI Studio."
        return True, f"Couldn't fully check the key (Google answered {e.code}); saved it anyway."
    except OSError:
        return True, "No internet connection to check the key; saved it anyway."


def save_key(key: str) -> None:
    lines = []
    src = ENV_FILE if os.path.exists(ENV_FILE) else os.path.join(ROOT, ".env.example")
    if os.path.exists(src):
        with open(src, encoding="utf-8-sig") as f:
            lines = [line.rstrip("\n") for line in f]
    lines = [line for line in lines if not re.match(r"\s*GOOGLE_API_KEY\s*=", line)]
    lines.insert(0, f"GOOGLE_API_KEY={key}")
    tmp = ENV_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, ENV_FILE)


def api_ready() -> bool:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{API_PORT}/health", timeout=1).close()
        return True
    except OSError:
        return False


class Handler(http.server.SimpleHTTPRequestHandler):
    """Serves web/ plus a local-only key API for the console's "Connect Gemini" dialog."""

    def log_message(self, *args):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _json(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/key-status":                  # never returns the key itself
            return self._json(200, {"has_key": env_has_key(), "agent_ready": api_ready() and not restart_api.is_set()})
        if path == "/":
            self.send_response(302)
            self.send_header("Location", f"/console.html?api=http://127.0.0.1:{API_PORT}")
            self.end_headers()
            return None
        return super().do_GET()

    def do_POST(self):
        if self.path.split("?")[0] != "/api/key":
            return self._json(404, {"ok": False, "message": "not found"})
        if self.headers.get("Origin") not in UI_ORIGINS or "application/json" not in (self.headers.get("Content-Type") or ""):
            return self._json(403, {"ok": False, "message": "forbidden"})
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 4096)
            key = str(json.loads(self.rfile.read(length) or b"{}").get("key", "")).strip().strip('"').strip("'")
        except (ValueError, AttributeError):
            return self._json(400, {"ok": False, "message": "Bad request."})
        if not KEY_RE.match(key):
            return self._json(400, {"ok": False, "message": "That doesn't look like an API key."})
        valid, note = check_key(key)
        if not valid:
            return self._json(400, {"ok": False, "message": note})
        save_key(key)
        restart_api.set()
        print("Gemini key saved to .env. Restarting the API...", flush=True)
        return self._json(200, {"ok": True, "message": note if note != "ok" else "Key saved."})


# ---------------------------------------------------------------- processes
def start_api(env: dict) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-m", "dispatch.api", "--port", str(API_PORT)], cwd=ROOT, env=env)


def stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def wait_api(seconds: float = 60) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if api_ready():
            return True
        time.sleep(0.5)
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Start Dispatch v2 (API + console).")
    ap.add_argument("--fresh", action="store_true", help="wipe the database and load a fresh demo day")
    ap.add_argument("--no-open", action="store_true", help="don't open a browser tab")
    ap.add_argument("--no-docker", action="store_true", help="don't start Docker services")
    args = ap.parse_args()

    vpy = venv_python()                                   # re-run inside the project venv
    if vpy and os.path.realpath(sys.prefix) != os.path.realpath(os.path.join(ROOT, ".venv")):
        return subprocess.call([vpy, os.path.abspath(__file__), *sys.argv[1:]])
    try:
        import dispatch.api  # noqa: F401
    except ImportError as e:
        print(f"Missing packages ({e}). Run: pip install -r requirements.txt")
        return 1

    for port in (API_PORT, UI_PORT):
        if port_open(port):
            print(f"Port {port} is already in use. Stop the other process or set API_PORT / UI_PORT.")
            return 1
    if not os.path.exists(ENV_FILE) and os.path.exists(os.path.join(ROOT, ".env.example")):
        shutil.copy(os.path.join(ROOT, ".env.example"), ENV_FILE)

    env = dict(os.environ, DEMO_MODE="1", CORS_ORIGINS=",".join(UI_ORIGINS), PYTHONUNBUFFERED="1")
    if "DATABASE_URL" not in os.environ:
        if not args.no_docker:
            start_docker(want_osrm=os.path.exists(OSRM_DATA))
        if not args.no_docker and wait_port(5432, 30) and wait_port(5432, 1):
            env["DATABASE_URL"] = PG_URL
            print("Database: Postgres (Docker).")
        else:
            env["DATABASE_URL"] = "sqlite:///" + os.path.join(ROOT, "dispatch_dev.db").replace("\\", "/")
            print("Database: SQLite (dispatch_dev.db).")
    print("Routing: " + ("OSRM (real roads)." if port_open(5000) else "straight-line estimate (OSRM not running)."))

    for attempt in range(10):                             # Postgres may still be finishing its first start
        r = subprocess.run([sys.executable, "-m", "dispatch.demo", "reset" if args.fresh else "seed"], cwd=ROOT, env=env,
                           capture_output=True, text=True)
        if r.returncode == 0:
            print(r.stdout.strip())
            break
        time.sleep(2)
    else:
        print("Couldn't prepare the database:\n" + (r.stderr or r.stdout)[-1500:])
        return 1
    if not env_has_key():
        print("No Gemini key yet: the console will ask for it. (Get one at https://aistudio.google.com/apikey)")

    def _stop(*_):
        raise KeyboardInterrupt
    for name in ("SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _stop)

    ui = http.server.ThreadingHTTPServer(("127.0.0.1", UI_PORT), functools.partial(Handler, directory=WEB_DIR))
    threading.Thread(target=ui.serve_forever, daemon=True).start()
    api = start_api(env)
    try:
        print("Starting the API...", flush=True)
        if not wait_api():
            print("The API did not start. See the log above.")
            return 1
        url = f"http://127.0.0.1:{UI_PORT}/console.html?api=http://127.0.0.1:{API_PORT}"
        print(f"\nDispatch console: {url}\nAPI docs:         http://127.0.0.1:{API_PORT}/docs\nCtrl+C to stop.\n", flush=True)
        if not args.no_open:
            webbrowser.open(url)
        while True:
            if restart_api.is_set():                      # new key saved from the console
                stop(api)
                api = start_api(env)
                wait_api()
                restart_api.clear()
                print("API restarted with the new key.", flush=True)
            elif api.poll() is not None:
                return api.returncode or 0
            time.sleep(0.3)
    except KeyboardInterrupt:
        print("\nStopping...")
        return 0
    finally:
        stop(api)
        ui.shutdown()


if __name__ == "__main__":
    sys.exit(main())
