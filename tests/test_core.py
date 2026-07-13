import json
from pathlib import Path

import jsonschema
import pytest
from PIL import Image

import vepay_api_core


def test_normalizers_handle_venezuelan_receipt_values():
    assert vepay_api_core.extract_amount_from_value("Bs. 2.500,00") == ("2.500,00", "2500.00")
    assert vepay_api_core.extract_amount_from_value("963.89 Bs") == ("963.89", "963.89")
    assert vepay_api_core.normalize_phone("+58 (412) 000-0000") == "584120000000"
    assert vepay_api_core.normalize_document_id("V-12.345.678") == "V-12345678"
    assert vepay_api_core.normalize_bank("0102 - Banco de Venezuela") == "0102 - BANCO DE VENEZUELA"
    assert vepay_api_core.normalize_concept("pago movil!") == "PAGO MOVIL"
    assert vepay_api_core.parse_date_time("01/01/2026 12:00 PM")["iso"] == "2026-01-01T12:00:00"


def test_detect_bank_prioritizes_bdv_app_over_recipient_bank():
    text = """
PagomovilBDV Personas
Fecha: 03/05/2026
Banco: 0172 - BANCAMIGA BANCO
UNIVERSAL, C.A.
Concepto: PAGO
"""

    assert vepay_api_core.detect_bank(text) == "bdv"


def test_detect_bank_uses_banesco_origin_bank_over_bancamiga_recipient():
    text = """
BANCO EMISOR
BANESCO BANCO UNIVERSAL S.A.C.A.
BANCO RECEPTOR
BANCAMIGA BANCO UNIVERSAL, C.A.
"""
    reversed_text = """
BANCO RECEPTOR
BANCAMIGA BANCO UNIVERSAL, C.A.
BANCO EMISOR
BANESCO BANCO UNIVERSAL S.A.C.A.
"""

    assert vepay_api_core.detect_bank(text) == "banesco"
    assert vepay_api_core.detect_bank(reversed_text) == "banesco"
    assert vepay_api_core.detect_bank_from_name("BANESCO BANCO UNIVERSAL S.A.C.A.") == "banesco"
    assert vepay_api_core.detect_bank_from_name("BANCAMIGA BANCO UNIVERSAL, C.A.") == "bancamiga"


def test_detect_bank_ignores_counterparty_banco_de_venezuela():
    bancamiga_text = """
Transaccion exitosa
Bancamiga
Banco Universal
NUMERO DE REFERENCIA:
170827492477
BANCO:
BANCO DE VENEZUELA
"""
    mercantil_text = """
Listo!
Tu Tpago fue exitoso
Banco destino:
0102 - Banco De Venezuela S.a.c.a. Banco Universal
Tpago
"""

    assert vepay_api_core.detect_bank(bancamiga_text) == "bancamiga"
    assert vepay_api_core.detect_bank(mercantil_text) == "mercantil"
    assert vepay_api_core.detect_bank("BANCO:\nBANCO DE VENEZUELA") is None


@pytest.mark.parametrize(
    ("ocr_text", "expected_bank"),
    [
        (
            """
Transaccion exitosa
Bancamiga
Banco Universal
BANCO:
BANCO DE VENEZUELA
""",
            "bancamiga",
        ),
        (
            """
Operacion Exitosa!
BANCO EMISOR
BANESCO BANCO UNIVERSAL S.A.C.A.
BANCO RECEPTOR
BANESCO BANCO UNIVERSAL S.A.C.A.
""",
            "banesco",
        ),
        (
            """
Operacion Exitosa!
BANCO EMISOR
BANESCO BANCO UNIVERSAL S.A.C.A.
BANCO RECEPTOR
BANCAMIGA BANCO UNIVERSAL, C.A.
""",
            "banesco",
        ),
        (
            """
PagomovilBDV Personas
Banco: 0102 - BANCO DE VENEZUELA
""",
            "bdv",
        ),
        (
            """
PagomovilBDV Personas
Banco: 0172 - BANCAMIGA BANCO
""",
            "bdv",
        ),
        (
            """
Listo!
Tu Tpago fue exitoso
Banco destino:
0102 - Banco De Venezuela S.a.c.a. Banco Universal
Tpago
""",
            "mercantil",
        ),
        (
            """
El dinero fue enviado
Dinero Rapido
BBVA Provincial
Banco: BANCO VENEZUELA
""",
            "provincial",
        ),
        (
            """
Pagar
El dinero fue enviado
Dinero Rapido
BBVA Provincial
Pago a:
FRANCISCO SILVA
""",
            "provincial",
        ),
    ],
)
def test_detect_bank_contextual_receipt_samples(ocr_text, expected_bank):
    assert vepay_api_core.detect_bank(ocr_text) == expected_bank


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

    schema = json.loads(Path("schemas/payment_receipt_schema.json").read_text(encoding="utf-8"))
    jsonschema.validate(receipt, schema)
    assert receipt["payment"]["bank_app"] == "bdv"
    assert receipt["payment"]["amount"]["value"] == "2500.00"
    assert receipt["recipient"]["document_id"] == "V-12345678"
    assert receipt["validation"]["is_complete"] is True
    assert "raw_text" not in receipt["ocr"]


def test_parse_bdv_runs_amount_crop_when_recipient_bank_mentions_bancamiga(
    monkeypatch,
    workspace_tmp,
):
    image_path = workspace_tmp / "bdv-test.jpeg"
    image_path.write_bytes(b"fake image bytes")
    primary_text = """
PagomovilBDV Personas
Fecha: 03/05/2026
Operacion: 005901670379
Identificacion: 30759313
Origen: 0102****3488
Destino: 04245750659
Banco: 0172 - BANCAMIGA BANCO
UNIVERSAL, C.A.
Concepto: PAGO
"""

    def fake_run_tesseract(*args, **kwargs):
        if kwargs.get("psm") == 7:
            return "963,89 Bs"
        return primary_text

    monkeypatch.setattr(vepay_api_core, "run_tesseract", fake_run_tesseract)
    monkeypatch.setattr(
        vepay_api_core,
        "pillow_threshold_crop",
        lambda *args, **kwargs: True,
    )

    receipt = vepay_api_core.parse_receipt_image(
        image_path,
        vepay_api_core.ReceiptOptions(tesseract_path="tesseract", include_raw_text=True),
    )

    assert receipt["payment"]["bank_app"] == "bdv"
    assert receipt["payment"]["amount"] == {
        "value": "963.89",
        "currency": "VES",
        "raw": "963,89",
    }
    assert "bdv_amount_crop" in receipt["ocr"]["passes"]
    assert "payment.amount.value" not in receipt["validation"]["missing_fields"]


def test_parse_banesco_uses_emitter_bank_and_destination_phone(monkeypatch, workspace_tmp):
    image_path = workspace_tmp / "banesco-test.jpeg"
    image_path.write_bytes(b"fake image bytes")
    ocr_text = """
Operacion Exitosa!
NUMERO DE REFERENCIA
061235636238
FECHA
03/05/2026 05:00:17PM
NUMERO CELULAR DE ORIGEN
04****0659
NUMERO CELULAR DE DESTINO
0424-5750659
IDENTIFICACION RECEPTOR
V-30759313
BANCO EMISOR
BANESCO BANCO UNIVERSAL S.A.C.A.
BANCO RECEPTOR
BANCAMIGA BANCO UNIVERSAL, C.A.
MONTO DE LA OPERACION
Bs. 963,89
CONCEPTO
PAGO
"""

    monkeypatch.setattr(vepay_api_core, "run_tesseract", lambda *args, **kwargs: ocr_text)

    receipt = vepay_api_core.parse_receipt_image(
        image_path,
        vepay_api_core.ReceiptOptions(tesseract_path="tesseract", include_raw_text=False),
    )

    assert receipt["payment"]["bank_app"] == "banesco"
    assert receipt["origin"]["bank"] == "BANESCO BANCO UNIVERSAL S.A.C.A"
    assert receipt["recipient"]["bank"] == "BANCAMIGA BANCO UNIVERSAL, C.A"
    assert receipt["recipient"]["phone"] == "04245750659"
    assert receipt["payment"]["amount"]["value"] == "963.89"
    assert receipt["validation"]["is_complete"] is True


def test_generic_numero_celular_does_not_capture_origin_phone_label():
    lines = [
        "NUMERO CELULAR DE ORIGEN",
        "04****0659",
        "NUMERO CELULAR DE DESTINO",
        "0424-5750659",
    ]

    assert (
        vepay_api_core.value_after_label(
            lines,
            vepay_api_core.LABEL_ALIASES["recipient_phone"],
        )
        == "0424-5750659"
    )


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

