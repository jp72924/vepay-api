from http.client import HTTPMessage

from scripts import start_client


def test_build_upstream_url_strips_api_prefix():
    url = start_client.build_upstream_url(
        "/api/v1/capabilities?x=1",
        "https://vepay-api.fly.dev/",
    )

    assert url == "https://vepay-api.fly.dev/v1/capabilities?x=1"


def test_build_upstream_url_can_target_local_api():
    url = start_client.build_upstream_url(
        "/api/healthz",
        "http://127.0.0.1:8080",
    )

    assert url == "http://127.0.0.1:8080/healthz"


def test_filtered_request_headers_remove_hop_by_hop_values():
    headers = HTTPMessage()
    headers.add_header("Host", "127.0.0.1:8765")
    headers.add_header("Connection", "keep-alive")
    headers.add_header("Content-Type", "multipart/form-data")

    filtered = start_client.filtered_request_headers(headers)

    assert filtered == {"Content-Type": "multipart/form-data"}


def test_standalone_client_config_uses_proxy_prefix():
    config = (start_client.CLIENT_DIR / "config.js").read_text(encoding="utf-8")

    assert 'apiPrefix: "/api"' in config
    assert 'mode: "standalone"' in config
