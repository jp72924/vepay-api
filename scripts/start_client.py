"""Serve the local VEPay API audit client and proxy API requests."""

from __future__ import annotations

import argparse
import http.server
import os
import posixpath
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
CLIENT_DIR = ROOT / "client"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_API_BASE_URL = "https://vepay-api.fly.dev/"
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def normalized_base_url(value: str) -> str:
    return value.rstrip("/") + "/"


def build_upstream_url(path: str, base_url: str) -> str:
    parsed = urllib.parse.urlsplit(path)
    api_path = parsed.path
    if api_path == "/api":
        stripped = "/"
    elif api_path.startswith("/api/"):
        stripped = api_path[len("/api") :]
    else:
        raise ValueError("Only /api paths can be proxied.")
    stripped = posixpath.normpath(stripped)
    if not stripped.startswith("/"):
        stripped = f"/{stripped}"
    if parsed.path.endswith("/") and not stripped.endswith("/"):
        stripped = f"{stripped}/"
    upstream = urllib.parse.urljoin(normalized_base_url(base_url), stripped.lstrip("/"))
    return urllib.parse.urlunsplit(
        urllib.parse.urlsplit(upstream)._replace(query=parsed.query)
    )


def filtered_request_headers(headers: http.client.HTTPMessage) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in headers.items():
        lowered = key.lower()
        if lowered in HOP_BY_HOP_HEADERS or lowered == "host":
            continue
        result[key] = value
    return result


def filtered_response_headers(headers: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for key, value in headers:
        lowered = key.lower()
        if lowered in HOP_BY_HOP_HEADERS:
            continue
        if lowered in {"server", "date"}:
            continue
        result.append((key, value))
    return result


class AuditClientHandler(http.server.SimpleHTTPRequestHandler):
    api_base_url = DEFAULT_API_BASE_URL

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(CLIENT_DIR), **kwargs)

    def do_OPTIONS(self) -> None:
        if self.path.startswith("/api"):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()
            return
        super().do_OPTIONS()

    def do_GET(self) -> None:
        if self.path.startswith("/api"):
            self.proxy_request()
            return
        super().do_GET()

    def do_POST(self) -> None:
        if self.path.startswith("/api"):
            self.proxy_request()
            return
        self.send_error(405, "Method not allowed")

    def proxy_request(self) -> None:
        try:
            upstream_url = build_upstream_url(self.path, self.api_base_url)
        except ValueError as exc:
            self.send_error(400, str(exc))
            return

        content_length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(content_length) if content_length else None
        request = urllib.request.Request(
            upstream_url,
            data=body,
            headers=filtered_request_headers(self.headers),
            method=self.command,
        )

        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = response.read()
                self.send_response(response.status)
                for key, value in filtered_response_headers(response.headers.items()):
                    self.send_header(key, value)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(payload)
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            self.send_response(exc.code)
            for key, value in filtered_response_headers(exc.headers.items()):
                self.send_header(key, value)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(payload)
        except urllib.error.URLError as exc:
            message = f'{{"detail":"Upstream API is unavailable: {exc.reason}"}}'
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(message.encode("utf-8"))

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, format: str, *args) -> None:
        sys.stderr.write(f"{self.address_string()} - {format % args}\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve the local VEPay API audit client.")
    parser.add_argument("--host", default=os.getenv("VEPAY_CLIENT_HOST", DEFAULT_HOST))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("VEPAY_CLIENT_PORT", str(DEFAULT_PORT))),
    )
    parser.add_argument(
        "--api-base-url",
        default=os.getenv("VEPAY_API_BASE_URL", DEFAULT_API_BASE_URL),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not CLIENT_DIR.exists():
        raise RuntimeError(f"Client directory not found: {CLIENT_DIR}")

    handler_class = type(
        "ConfiguredAuditClientHandler",
        (AuditClientHandler,),
        {"api_base_url": normalized_base_url(args.api_base_url)},
    )
    server = http.server.ThreadingHTTPServer((args.host, args.port), handler_class)
    url = f"http://{args.host}:{args.port}"
    print(f"Serving VEPay API audit client: {url}")
    print(f"Proxying /api/* to: {handler_class.api_base_url}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
