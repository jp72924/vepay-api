import base64
import time
from pathlib import Path

from fastapi.testclient import TestClient

import vepay_api


def sample_receipt(filename: str = "receipt.jpg", include_raw_text: bool = False):
    receipt = {
        "schema_version": "vepay_api_receipt_v1",
        "source": {
            "file_name": filename,
            "file_path": str(Path.cwd() / "private" / filename),
            "sha256": "0" * 64,
        },
        "payment": {
            "bank_app": "bdv",
            "status": "success",
            "reference": "123456789012",
            "amount": {"value": "2500.00", "currency": "VES", "raw": "2.500,00"},
            "date_time": {"raw": "01/01/2026 12:00 PM", "iso": "2026-01-01T12:00:00"},
            "concept": "PAGO",
        },
        "origin": {"phone": None, "account": None, "bank": None},
        "recipient": {
            "phone": "04120000000",
            "document_id": "V-12345678",
            "bank": "0102 - BANCO DE VENEZUELA",
        },
        "ocr": {"engine": "tesseract", "language": "spa+eng", "passes": ["full_psm_6"]},
        "transaction_key": "1" * 64,
        "validation": {"is_complete": True, "missing_fields": [], "warnings": []},
    }
    if include_raw_text:
        receipt["ocr"]["raw_text"] = "raw OCR text"
    return receipt


def install_fake_ocr(monkeypatch):
    monkeypatch.setattr(vepay_api, "get_tesseract_path", lambda: "tesseract")

    def fake_parse(path, options):
        return sample_receipt(Path(path).name, include_raw_text=options.include_raw_text)

    monkeypatch.setattr(vepay_api, "parse_receipt_image", fake_parse)


def test_healthz_degrades_without_tesseract(monkeypatch):
    monkeypatch.setattr(
        vepay_api,
        "get_tesseract_path",
        lambda: (_ for _ in ()).throw(RuntimeError("missing tesseract")),
    )
    client = TestClient(vepay_api.app)

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"


def test_healthz_does_not_expose_internal_paths(monkeypatch):
    monkeypatch.setattr(
        vepay_api,
        "get_tesseract_path",
        lambda: r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    )
    monkeypatch.setattr(vepay_api, "list_tesseract_languages", lambda path: ["eng", "spa"])
    client = TestClient(vepay_api.app)

    response = client.get("/healthz")
    body = response.json()
    serialized = response.text

    assert response.status_code == 200
    assert body["app"] == "VEPay API"
    assert "tesseract_path" not in body
    assert "C:\\" not in serialized
    assert "/usr/" not in serialized
    assert "Program Files" not in serialized


def test_root_uses_rebranded_identity():
    client = TestClient(vepay_api.app)

    response = client.get("/")

    assert response.status_code == 200
    assert response.json()["app"] == "VEPay API"


def test_multipart_parse_scrubs_server_paths_and_omits_raw_text(monkeypatch):
    install_fake_ocr(monkeypatch)
    client = TestClient(vepay_api.app)

    response = client.post(
        "/v1/receipts/parse",
        files=[("files", ("receipt.jpg", b"fake image", "image/jpeg"))],
    )

    body = response.json()
    assert response.status_code == 200
    assert body["summary"] == {"total": 1, "complete": 1, "incomplete": 0, "errors": 0}
    assert body["receipts"][0]["source"]["file_path"].startswith("upload://")
    assert "private" not in body["receipts"][0]["source"]["file_path"]
    assert "raw_text" not in body["receipts"][0]["ocr"]


def test_json_parse_accepts_base64_and_can_return_raw_text(monkeypatch):
    install_fake_ocr(monkeypatch)
    client = TestClient(vepay_api.app)
    payload = {
        "images": [
            {
                "filename": "receipt.jpg",
                "content_type": "image/jpeg",
                "image_base64": base64.b64encode(b"fake image").decode("ascii"),
            }
        ],
        "include_raw_text": True,
    }

    response = client.post("/v1/receipts/parse-json", json=payload)

    body = response.json()
    assert response.status_code == 200
    assert body["receipts"][0]["ocr"]["raw_text"] == "raw OCR text"
    assert body["receipts"][0]["source"]["file_path"].startswith("upload://")


def test_invalid_upload_is_reported_per_file_without_tesseract(monkeypatch):
    monkeypatch.setattr(
        vepay_api,
        "get_tesseract_path",
        lambda: (_ for _ in ()).throw(RuntimeError("should not be called")),
    )
    client = TestClient(vepay_api.app)

    response = client.post(
        "/v1/receipts/parse",
        files=[("files", ("receipt.txt", b"fake text", "text/plain"))],
    )

    body = response.json()
    assert response.status_code == 200
    assert body["summary"] == {"total": 1, "complete": 0, "incomplete": 0, "errors": 1}
    assert body["errors"][0]["code"] == "unsupported_extension"


def test_api_key_can_be_required(monkeypatch):
    install_fake_ocr(monkeypatch)
    monkeypatch.setattr(vepay_api, "REQUIRE_API_KEY", True)
    monkeypatch.setattr(vepay_api, "API_KEY", "secret")
    client = TestClient(vepay_api.app)

    response = client.post(
        "/v1/receipts/parse",
        files=[("files", ("receipt.jpg", b"fake image", "image/jpeg"))],
    )

    assert response.status_code == 401

    authorized = client.post(
        "/v1/receipts/parse",
        headers={"X-API-Key": "secret"},
        files=[("files", ("receipt.jpg", b"fake image", "image/jpeg"))],
    )
    assert authorized.status_code == 200


def test_jobs_endpoint_returns_trackable_job(monkeypatch):
    install_fake_ocr(monkeypatch)
    client = TestClient(vepay_api.app)
    payload = {
        "images": [
            {
                "filename": "receipt.jpg",
                "content_type": "image/jpeg",
                "image_base64": base64.b64encode(b"fake image").decode("ascii"),
            }
        ]
    }

    created = client.post("/v1/jobs", json=payload)
    assert created.status_code == 202
    job_id = created.json()["job_id"]

    status = None
    for _ in range(10):
        status = client.get(f"/v1/jobs/{job_id}")
        if status.json()["status"] == "succeeded":
            break
        time.sleep(0.05)

    assert status is not None
    assert status.status_code == 200
    assert status.json()["status"] in {"queued", "running", "succeeded"}

