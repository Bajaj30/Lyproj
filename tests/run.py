"""
One command for every check in the project.

    python tests/run.py              unit + api, and browser if it can run
    python tests/run.py unit         pipeline + security building blocks (no server)
    python tests/run.py api          93+ end-to-end API checks against a live server
    python tests/run.py browser      the screens, in headless Chromium

Each server suite gets its own throwaway server: a temp directory holds its
database, uploads, read cache and spend ledger, so a run never touches real
patient images or real spend. The reader is forced to the MOCK even if a real
API key is configured: these checks must never cost anything. Medicine data is
the labelled fixture in ml_pipeline/tests/fixtures/india.

Run with the project's virtualenv:  .venv/bin/python tests/run.py
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
ADMIN_EMAIL, ADMIN_PASSWORD = "admin@example.com", "Vetting-Desk-71-Lantern"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    """A throwaway backend: its own database, uploads, cache and log."""

    def __init__(self, serve_ui: bool = False):
        self.tmp = Path(tempfile.mkdtemp(prefix="prescriptai-test-"))
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.db, self.log = self.tmp / "test.db", self.tmp / "server.log"
        self.env = {
            **os.environ,
            "DATABASE_URL": f"sqlite:///{self.db}",
            "PRESCRIPTAI_DATA_DIR": str(ROOT / "ml_pipeline" / "tests" / "fixtures" / "india"),
            "PRESCRIPTAI_UPLOAD_DIR": str(self.tmp / "uploads"),
            "PRESCRIPTAI_CACHE_DIR": str(self.tmp / "cache"),
            "SECRET_KEY": "test-secret-key-for-local-checks-only",
            "READER": "mock",                       # never a paid call from a test
            "REQUIRE_VETTING": "true",
            "PAGE_READS_PER_DOCTOR_PER_DAY": "15",
            "RATE_LIMIT_BACKEND": "database",
            "SMS_DELIVERY": "console", "SMTP_HOST": "",   # codes go to the log, where the tests read them
            "COOKIE_SECURE": "false",
            "CORS_ORIGINS": self.base, "PUBLIC_BASE_URL": self.base,
        }
        self.env.pop("DATA_ENCRYPTION_KEY", None)
        self.env.pop("WEB_DIST", None)
        if serve_ui:
            self.env["WEB_DIST"] = str(ROOT / "web_app" / "dist")
        self.proc = None

    def create_admin(self) -> None:
        out = subprocess.run([PY, "manage.py", "create-admin", "--email", ADMIN_EMAIL, "--name", "Site Admin"],
                             cwd=ROOT / "backend", env={**self.env, "ADMIN_PASSWORD": ADMIN_PASSWORD},
                             capture_output=True, text=True)
        if out.returncode != 0:
            raise SystemExit(f"create-admin failed:\n{out.stdout}{out.stderr}")

    def start(self) -> None:
        self.proc = subprocess.Popen([PY, "-m", "uvicorn", "main:app", "--port", str(self.port)],
                                     cwd=ROOT / "backend", env=self.env,
                                     stdout=open(self.log, "a"), stderr=subprocess.STDOUT)
        for _ in range(120):
            try:
                urllib.request.urlopen(f"{self.base}/api/health", timeout=1)
                return
            except Exception:
                if self.proc.poll() is not None:
                    break
                time.sleep(0.25)
        raise SystemExit(f"The test server did not start. Log:\n{self.log.read_text()[-3000:]}")

    def stop(self) -> None:
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None

    def suite_env(self) -> dict:
        return {**os.environ, "E2E_BASE": self.base, "E2E_LOG": str(self.log), "E2E_DB": str(self.db),
                "E2E_TMP": str(self.tmp), "E2E_STATE": str(self.tmp / "state.json"),
                "E2E_ADMIN_EMAIL": ADMIN_EMAIL, "E2E_ADMIN_PASSWORD": ADMIN_PASSWORD}


def banner(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}", flush=True)


def run_unit() -> bool:
    ok = True
    fixture = {**os.environ, "PRESCRIPTAI_DATA_DIR": str(ROOT / "ml_pipeline" / "tests" / "fixtures" / "india")}
    for title, cmd, cwd in (
        ("unit: dispensing comparison", [PY, "-m", "tests.test_dispense"], ROOT / "ml_pipeline"),
        ("unit: API-calling layer (fake client, no key)", [PY, "-m", "tests.test_reader"], ROOT / "ml_pipeline"),
        ("unit: encryption, rate limiter, SMS delivery", [PY, "tests/test_security.py"], ROOT),
    ):
        banner(title)
        ok = subprocess.run(cmd, cwd=cwd, env=fixture).returncode == 0 and ok
    return ok


def after_restart(server: Server) -> bool:
    """Things that must survive a restart of the API process."""
    import httpx

    banner("api: after a restart")
    state = json.loads((server.tmp / "state.json").read_text())
    server.stop()
    server.start()
    results = []

    def check(name, cond):
        results.append(bool(cond))
        print(("PASS " if cond else "FAIL ") + name)

    c = httpx.Client(base_url=server.base, headers={"X-Requested-With": "PrescriptAI"}, timeout=30)
    r = c.post("/api/auth/login", json={"identifier": state["chem2"], "password": state["password"]})
    check("sessions and accounts survive the restart", r.status_code == 200)
    r = c.post("/api/pharmacy/redeem", json={"otp": "654321"})
    check("a locked-out pharmacy is STILL locked out after the restart (limits live in the database)",
          r.status_code == 429)
    log = server.log.read_text()
    check("the restart applied no migration and adopted nothing (already tracked)", "adopted" not in log)
    check("no model has changed without a migration", "without a migration" not in log)
    # The limiter falls back to per-process counting if it cannot reach the
    # database. That fallback hid a real bug once (it waited on a lock its own
    # request was holding), so its use during a run is itself a failure.
    check("the rate limiter never fell back to in-memory counting", "[RATELIMIT]" not in log)
    check("no unhandled exception in the server log", "Traceback" not in log)
    print(f"\n{sum(results)}/{len(results)} passed")
    if not all(results):
        print(f"server log: {server.log}")
    return all(results)


def run_api() -> bool:
    banner("api: end-to-end against a live server (mock reader, fixture data)")
    s = Server()
    s.create_admin()
    s.start()
    try:
        ok = subprocess.run([PY, "tests/e2e_api.py"], cwd=ROOT, env=s.suite_env()).returncode == 0
        return after_restart(s) and ok
    finally:
        s.stop()


def run_browser(required: bool) -> bool:
    banner("browser: the screens, in headless Chromium")
    try:
        import playwright  # noqa: F401
    except ImportError:
        print("SKIPPED: Playwright is not installed.  pip install playwright && playwright install chromium")
        return not required
    if not (ROOT / "web_app" / "dist" / "index.html").is_file():
        print("SKIPPED: the UI is not built.  cd web_app && VITE_API_URL= npm run build")
        return not required
    s = Server(serve_ui=True)
    s.create_admin()
    s.start()
    try:
        ok = subprocess.run([PY, "tests/e2e_browser.py"], cwd=ROOT, env=s.suite_env()).returncode == 0
        print(f"screenshots: {s.tmp}/shot_*.png")
        return ok
    finally:
        s.stop()


def main() -> int:
    wanted = sys.argv[1:] or ["unit", "api", "browser"]
    explicit = bool(sys.argv[1:])
    outcome = {}
    if "unit" in wanted:
        outcome["unit"] = run_unit()
    if "api" in wanted:
        outcome["api"] = run_api()
    if "browser" in wanted:
        outcome["browser"] = run_browser(required=explicit)
    banner("summary")
    for name, ok in outcome.items():
        print(f"  {name:8} {'ok' if ok else 'FAILED'}")
    return 0 if all(outcome.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
