"""Start VEPay API in the background with logs and a PID file."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = ROOT / ".vepay-api-tmp"
PID_FILE = RUNTIME_DIR / "uvicorn.pid"
LOG_OUT = RUNTIME_DIR / "uvicorn.out.log"
LOG_ERR = RUNTIME_DIR / "uvicorn.err.log"
HOST = os.getenv("VEPAY_API_HOST", "127.0.0.1")
PORT = int(os.getenv("VEPAY_API_PORT", "8080"))
BASE_URL = f"http://{HOST}:{PORT}"
TIMEOUT_SECONDS = int(os.getenv("VEPAY_API_START_TIMEOUT", "20"))


def is_pid_running(pid: int) -> bool:
    if os.name == "nt":
        return listener_pid() == pid
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def get_json(path: str) -> dict:
    with urllib.request.urlopen(f"{BASE_URL}{path}", timeout=2) as response:
        return json.loads(response.read().decode("utf-8"))


def listener_pid() -> int | None:
    result = subprocess.run(
        ["netstat", "-ano"],
        check=False,
        capture_output=True,
        text=True,
    )
    for line in result.stdout.splitlines():
        if f":{PORT}" not in line or "LISTENING" not in line:
            continue
        parts = line.split()
        if not parts:
            continue
        try:
            return int(parts[-1])
        except ValueError:
            continue
    return None


def wait_for_health() -> dict:
    deadline = time.monotonic() + TIMEOUT_SECONDS
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return get_json("/healthz")
        except (OSError, urllib.error.URLError) as exc:
            last_error = exc
        time.sleep(0.5)
    raise RuntimeError(f"API did not become reachable: {last_error}")


def main() -> int:
    RUNTIME_DIR.mkdir(exist_ok=True)
    try:
        existing_health = get_json("/healthz")
    except Exception:
        existing_health = None
    if existing_health:
        if existing_health.get("app") != "VEPay API":
            raise RuntimeError(f"Port {PORT} is already used by another service.")
        existing_pid = listener_pid()
        if existing_pid:
            PID_FILE.write_text(str(existing_pid), encoding="utf-8")
            print(f"Already running: pid={existing_pid} url={BASE_URL}")
        else:
            print(f"Already reachable: url={BASE_URL}")
        print(f"health.status={existing_health.get('status')}")
        print(f"health.tesseract_available={existing_health.get('tesseract_available')}")
        return 0

    if PID_FILE.exists():
        try:
            existing_pid = int(PID_FILE.read_text(encoding="utf-8").strip())
        except ValueError:
            existing_pid = 0
        if existing_pid and is_pid_running(existing_pid):
            print(f"Already running: pid={existing_pid} url={BASE_URL}")
            return 0
        PID_FILE.unlink(missing_ok=True)

    env = os.environ.copy()
    env["VEPAY_API_TEMP_DIR"] = str(RUNTIME_DIR)

    creationflags = 0
    start_new_session = False
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        start_new_session = True

    stdout = LOG_OUT.open("a", encoding="utf-8")
    stderr = LOG_ERR.open("a", encoding="utf-8")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "vepay_api:app",
            "--host",
            HOST,
            "--port",
            str(PORT),
        ],
        cwd=ROOT,
        env=env,
        stdout=stdout,
        stderr=stderr,
        stdin=subprocess.DEVNULL,
        creationflags=creationflags,
        start_new_session=start_new_session,
    )
    stdout.close()
    stderr.close()
    PID_FILE.write_text(str(proc.pid), encoding="utf-8")

    try:
        health = wait_for_health()
    except Exception:
        try:
            if os.name == "nt":
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        PID_FILE.unlink(missing_ok=True)
        raise

    print(f"Started VEPay API: pid={proc.pid} url={BASE_URL}")
    print(f"health.status={health.get('status')}")
    print(f"health.tesseract_available={health.get('tesseract_available')}")
    print(f"stdout={LOG_OUT}")
    print(f"stderr={LOG_ERR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
