"""Finite live-server smoke test for the VEPay API."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = ROOT / ".vepay-api-tmp"
LOG_OUT = RUNTIME_DIR / "smoke-api.out.log"
LOG_ERR = RUNTIME_DIR / "smoke-api.err.log"
HOST = os.getenv("VEPAY_API_SMOKE_HOST", "127.0.0.1")
PORT = int(os.getenv("VEPAY_API_SMOKE_PORT", "8080"))
BASE_URL = f"http://{HOST}:{PORT}"
TIMEOUT_SECONDS = int(os.getenv("VEPAY_API_SMOKE_TIMEOUT", "20"))


def get_json(path: str) -> tuple[int, dict]:
    request = urllib.request.Request(f"{BASE_URL}{path}", method="GET")
    with urllib.request.urlopen(request, timeout=2) as response:
        body = response.read().decode("utf-8")
        return response.status, json.loads(body)


def wait_for_health() -> dict:
    deadline = time.monotonic() + TIMEOUT_SECONDS
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            status, payload = get_json("/healthz")
            if status == 200:
                return payload
        except (OSError, urllib.error.URLError) as exc:
            last_error = exc
        time.sleep(0.5)
    raise RuntimeError(f"API did not become reachable: {last_error}")


def main() -> int:
    RUNTIME_DIR.mkdir(exist_ok=True)
    env = os.environ.copy()
    env["VEPAY_API_TEMP_DIR"] = str(RUNTIME_DIR)

    with LOG_OUT.open("w", encoding="utf-8") as stdout, LOG_ERR.open(
        "w", encoding="utf-8"
    ) as stderr:
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
        )

        try:
            health = wait_for_health()
            capabilities_status, capabilities = get_json("/v1/capabilities")
            if capabilities_status != 200:
                raise RuntimeError("/v1/capabilities returned a non-200 response.")
            print(f"health.status={health.get('status')}")
            print(f"health.tesseract_available={health.get('tesseract_available')}")
            print(f"capabilities.schema_version={capabilities.get('schema_version')}")
            print(f"capabilities.max_files={capabilities.get('limits', {}).get('max_files')}")
            print(f"url={BASE_URL}")
            return 0
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
