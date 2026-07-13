"""Stop the background VEPay API process started by scripts/start_api.py."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = ROOT / ".vepay-api-tmp"
PID_FILE = RUNTIME_DIR / "uvicorn.pid"
HOST = os.getenv("VEPAY_API_HOST", "127.0.0.1")
PORT = int(os.getenv("VEPAY_API_PORT", "8080"))


def is_pid_running(pid: int) -> bool:
    if os.name == "nt":
        return listener_pid() == pid
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def stop_process(pid: int) -> None:
    if os.name == "nt":
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0 and is_pid_running(pid):
            subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
    else:
        os.kill(pid, signal.SIGTERM)


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


def main() -> int:
    if not PID_FILE.exists():
        pid = listener_pid()
        if not pid:
            print("No PID file found and no API listener was detected.")
            return 0
        stop_process(pid)
        print(f"Stopped VEPay API listener pid={pid}.")
        return 0

    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    except ValueError:
        pid = listener_pid()
        PID_FILE.unlink(missing_ok=True)
        if pid:
            stop_process(pid)
            print(f"Removed invalid PID file and stopped listener pid={pid}.")
        else:
            print("Removed invalid PID file.")
        return 0

    if not is_pid_running(pid):
        listener = listener_pid()
        PID_FILE.unlink(missing_ok=True)
        if listener:
            stop_process(listener)
            print(f"Process {pid} was stale; stopped listener pid={listener}.")
        else:
            print(f"Process {pid} is not running; removed PID file.")
        return 0

    stop_process(pid)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not is_pid_running(pid):
            PID_FILE.unlink(missing_ok=True)
            print(f"Stopped VEPay API pid={pid}.")
            return 0
        time.sleep(0.25)

    print(f"Stop requested, but pid={pid} still appears to be running.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
