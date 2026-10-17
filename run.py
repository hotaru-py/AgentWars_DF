#!/usr/bin/env python3
"""Start the Dispatch Console on any OS: ADK agent API (port 8000) + web UI (port 8001).

    python run.py              start everything and open the console in your browser
    python run.py --fresh      same, but first forget what the agent has learned
    python run.py --no-open    don't open a browser tab

Needs GOOGLE_API_KEY in dispatch_agent/.env (copied from .env.example on first run).
Uses the project's .venv automatically if there is one. Ctrl+C stops both servers.
Env overrides: API_PORT, UI_PORT. Standard library only.
"""

import argparse
import functools
import http.server
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
AGENT_DIR = os.path.join(ROOT, "dispatch_agent")
OUT_DIR = os.path.join(AGENT_DIR, "out")
ENV_FILE = os.path.join(AGENT_DIR, ".env")
API_PORT = int(os.environ.get("API_PORT", 8000))
UI_PORT = int(os.environ.get("UI_PORT", 8001))


def venv_python() -> str | None:
    sub = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
    path = os.path.join(ROOT, ".venv", *sub)
    return path if os.path.exists(path) else None


def has_api_key() -> bool:
    if os.environ.get("GOOGLE_API_KEY"):
        return True
    try:
        with open(ENV_FILE, encoding="utf-8") as f:
            return any(re.match(r"\s*GOOGLE_API_KEY\s*=\s*\S+", line) for line in f)
    except OSError:
        return False


def port_busy(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def wait_for_api(timeout: float = 40) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{API_PORT}/list-apps", timeout=1).close()
            return True
        except OSError:
            time.sleep(0.5)
    return False


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def end_headers(self):  # always serve fresh state.json
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def main() -> int:
    ap = argparse.ArgumentParser(description="Start the Dispatch Console.")
    ap.add_argument("--fresh", action="store_true", help="reset the learned policy first")
    ap.add_argument("--no-open", action="store_true", help="don't open a browser tab")
    args = ap.parse_args()

    # Re-run inside the project venv when it exists and we're not already in it.
    vpy = venv_python()
    if vpy and os.path.realpath(sys.prefix) != os.path.realpath(os.path.join(ROOT, ".venv")):
        return subprocess.call([vpy, os.path.abspath(__file__), *sys.argv[1:]])

    try:
        import google.adk  # noqa: F401
    except ImportError:
        print("google-adk is not installed. Run: pip install -r requirements.txt")
        return 1

    if not has_api_key():
        if not os.path.exists(ENV_FILE):
            shutil.copy(os.path.join(AGENT_DIR, ".env.example"), ENV_FILE)
        print(f"Set GOOGLE_API_KEY in {os.path.relpath(ENV_FILE, ROOT)} first "
              "(get a key at https://aistudio.google.com/apikey), then re-run.")
        return 1

    for port in (API_PORT, UI_PORT):
        if port_busy(port):
            print(f"Port {port} is already in use. Stop the other process or set API_PORT / UI_PORT.")
            return 1

    # The agent starts with a clean in-memory world, so drop the previous run's snapshot.
    os.makedirs(OUT_DIR, exist_ok=True)
    for name in ("state.json", *(("policy.json",) if args.fresh else ())):
        try:
            os.remove(os.path.join(OUT_DIR, name))
        except FileNotFoundError:
            pass
    if args.fresh:
        print("Learned policy reset.")

    # Route a plain kill / terminal close through the same cleanup as Ctrl+C.
    def _stop(*_):
        raise KeyboardInterrupt
    for name in ("SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _stop)

    ui = http.server.ThreadingHTTPServer(
        ("127.0.0.1", UI_PORT), functools.partial(QuietHandler, directory=AGENT_DIR))
    threading.Thread(target=ui.serve_forever, daemon=True).start()

    agent = subprocess.Popen(
        [sys.executable, "-m", "google.adk.cli", "api_server", "--port", str(API_PORT),
         f"--allow_origins=http://localhost:{UI_PORT}", f"--allow_origins=http://127.0.0.1:{UI_PORT}", "."],
        cwd=ROOT)
    try:
        print("Waiting for the agent API...", flush=True)
        if not wait_for_api():
            print("The agent API did not start. See the log above.")
            return 1
        if agent.poll() is not None:
            return agent.returncode or 1
        url = f"http://localhost:{UI_PORT}/console.html?api=http://localhost:{API_PORT}"
        print(f"\nDispatch Console: {url}\nCtrl+C to stop.\n", flush=True)
        if not args.no_open:
            webbrowser.open(url)
        agent.wait()
        return agent.returncode or 0
    except KeyboardInterrupt:
        print("\nStopping...")
        return 0
    finally:
        if agent.poll() is None:
            agent.terminate()
            try:
                agent.wait(timeout=5)
            except subprocess.TimeoutExpired:
                agent.kill()
        ui.shutdown()


if __name__ == "__main__":
    sys.exit(main())
