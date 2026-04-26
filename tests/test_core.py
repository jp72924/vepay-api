import json
from pathlib import Path

import jsonschema
from PIL import Image

import vepay_api_core


def test_normalizers_handle_venezuelan_receipt_values():
    assert vepay_api_core.extract_amount_from_value("Bs. 2.500,00") == ("2.500,00", "2500.00")
    assert vepay_api_core.normalize_phone("+58 (412) 000-0000") == "584120000000"
    assert vepay_api_core.normalize_document_id("V-12.345.678") == "V-12345678"
    assert vepay_api_core.normalize_bank("0102 - Banco de Venezuela") == "0102 - BANCO DE VENEZUELA"
    assert vepay_api_core.normalize_concept("pago movil!") == "PAGO MOVIL"
    assert vepay_api_core.parse_date_time("01/01/2026 12:00 PM")["iso"] == "2026-01-01T12:00:00"


def test_parse_receipt_image_builds_schema_valid_receipt(monkeypatch, workspace_tmp):
    image_path = workspace_tmp / "receipt.jpg"
    image_path.write_bytes(b"fake image bytes")
    ocr_text = """
BDV Personas
Transaccion exitosa
Numero de referencia
123456789012
Monto
2.500,00
Fecha
01/01/2026 12:00 PM
Banco
0102 - Banco de Venezuela
Concepto
Pago
Numero celular de destino
04120000000
Identificacion
V-12.345.678
"""

    monkeypatch.setattr(vepay_api_core, "run_tesseract", lambda *args, **kwargs: ocr_text)
    receipt = vepay_api_core.parse_receipt_image(
        image_path,
        vepay_api_core.ReceiptOptions(tesseract_path="tesseract", include_raw_text=False),
    )

    schema = json.loads(Path("payment_receipt_schema.json").read_text(encoding="utf-8"))
    jsonschema.validate(receipt, schema)
    assert receipt["payment"]["bank_app"] == "bdv"
    assert receipt["payment"]["amount"]["value"] == "2500.00"
    assert receipt["recipient"]["document_id"] == "V-12345678"
    assert receipt["validation"]["is_complete"] is True
    assert "raw_text" not in receipt["ocr"]


def test_pillow_threshold_crop_is_portable(workspace_tmp):
    source = workspace_tmp / "source.png"
    output = workspace_tmp / "crop.png"
    image = Image.new("RGB", (100, 100), (80, 80, 80))
    for x in range(10, 90):
        for y in range(30, 34):
            image.putpixel((x, y), (245, 245, 245))
    image.save(source)

    created = vepay_api_core.pillow_threshold_crop(
        source,
        output,
        rect_pct=(0.055, 0.286, 0.89, 0.08),
        threshold=170,
        scale=3,
    )

    assert created is True
    with Image.open(output) as cropped:
        assert cropped.size == (267, 24)

