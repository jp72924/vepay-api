#!/usr/bin/env python3
"""VEPay API: extract Venezuelan mobile payment receipts from screenshots."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "vepay_api_receipt_v1"
APP_NAME = "VEPay API"
APP_VERSION = "0.1.0"
DEFAULT_LANG = "spa+eng"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp"}

REQUIRED_EXTRACTION_FIELDS = (
    "payment.reference",
    "payment.amount.value",
    "payment.date_time.raw",
    "recipient.bank",
    "payment.concept",
)


@dataclass(frozen=True)
class ReceiptOptions:
    """Runtime options shared by the CLI and API layers."""

    tesseract_path: str
    lang: str = DEFAULT_LANG
    include_raw_text: bool = True
    enable_crops: bool = True


def strip_accents(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def norm(value: str) -> str:
    value = strip_accents(value).lower()
    value = re.sub(r"[^a-z0-9/*:.()#* -]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def clean_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r", "\n").split("\n"):
        line = re.sub(r"\s+", " ", raw).strip()
        if line:
            lines.append(line)
    return lines


def find_tesseract(custom_path: str | None = None) -> str:
    candidates = []
    if custom_path:
        candidates.append(custom_path)
    found = shutil.which("tesseract")
    if found:
        candidates.append(found)
    candidates.extend(
        [
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        ]
    )
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(Path(candidate))
    raise RuntimeError(
        "Tesseract no fue encontrado. Instala Tesseract OCR o pasa --tesseract RUTA."
    )


def run_tesseract(
    image_path: Path,
    tesseract_path: str,
    *,
    lang: str = DEFAULT_LANG,
    psm: int = 6,
) -> str:
    cmd = [
        tesseract_path,
        str(image_path),
        "stdout",
        "-l",
        lang,
        "--psm",
        str(psm),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0:
        raise RuntimeError(
            f"Tesseract fallo en {image_path.name}: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.stdout.strip()


def source_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


BDV_RECEIPT_TOKENS = (
    "pagomovilbdv",
    "pago movilbdv",
    "pagomovil bdv",
    "pago movil bdv",
    "bdv personas",
)

MERCANTIL_RECEIPT_TOKENS = (
    "tu tpago fue exitoso",
    "enviar tpago",
    "tpago",
    "mercantil",
)

PROVINCIAL_RECEIPT_TOKENS = (
    "dinero rapido",
    "bbva provincial",
)

COUNTERPARTY_BANK_LABELS = (
    "banco receptor",
    "banco destino",
    "banco",
)


def has_bdv_receipt_signal(text: str) -> bool:
    ntext = norm(text)
    return any(token in ntext for token in BDV_RECEIPT_TOKENS)


def has_any_token(text: str, tokens: tuple[str, ...]) -> bool:
    ntext = norm(text)
    return any(token in ntext for token in tokens)


def detect_bank_from_name(value: str | None) -> str | None:
    if not value:
        return None
    ntext = norm(value)
    if has_bdv_receipt_signal(value) or "banco de venezuela" in ntext:
        return "bdv"
    if "banesco" in ntext:
        return "banesco"
    if "bancamiga" in ntext:
        return "bancamiga"
    if "tpago" in ntext or "mercantil" in ntext:
        return "mercantil"
    if "bbva provincial" in ntext or (
        "dinero rapido" in ntext and "provincial" in ntext
    ):
        return "provincial"
    return None


def strip_counterparty_bank_sections(lines: list[str]) -> list[str]:
    """Remove destination-bank labels/values before receipt-level bank detection."""

    normalized_labels = [norm(label) for label in COUNTERPARTY_BANK_LABELS]
    normalized_lines = [norm(line) for line in lines]
    kept: list[str] = []
    index = 0
    while index < len(lines):
        normalized_line = normalized_lines[index]
        if any(
            line_matches_label(normalized_line, label)
            for label in normalized_labels
        ):
            index += 1
            while index < len(lines):
                next_normalized = normalized_lines[index]
                if is_label_header(next_normalized):
                    break
                index += 1
            continue
        kept.append(lines[index])
        index += 1
    return kept


def detect_bank_from_receipt_text(text: str) -> str | None:
    lines = clean_lines(text)
    app_text = "\n".join(strip_counterparty_bank_sections(lines))
    if has_bdv_receipt_signal(app_text):
        return "bdv"
    if has_any_token(app_text, MERCANTIL_RECEIPT_TOKENS):
        return "mercantil"
    if has_any_token(app_text, PROVINCIAL_RECEIPT_TOKENS):
        return "provincial"
    return detect_bank_from_name(app_text)


def detect_bank(text: str) -> str | None:
    lines = clean_lines(text)
    origin_bank = value_after_label(lines, ["banco emisor"])
    bank_from_origin = detect_bank_from_name(origin_bank)
    if bank_from_origin:
        return bank_from_origin
    return detect_bank_from_receipt_text(text)


def detect_status(text: str) -> str | None:
    ntext = norm(text)
    success_tokens = (
        "transaccion exitosa",
        "operacion exitosa",
        "fue exitoso",
        "el dinero fue enviado",
        "listo",
    )
    if any(token in ntext for token in success_tokens):
        return "success"
    return None


def value_after_label(lines: list[str], aliases: list[str]) -> str | None:
    normalized_aliases = [norm(alias) for alias in aliases]
    normalized_lines = [norm(line) for line in lines]
    for idx, normalized_line in enumerate(normalized_lines):
        matching_aliases = [
            alias for alias in normalized_aliases if line_matches_label(normalized_line, alias)
        ]
        if not matching_aliases:
            continue

        raw_line = lines[idx]
        candidates: list[str] = []
        if ":" in raw_line:
            candidates.append(raw_line.split(":", 1)[1].strip())

        # OCR sometimes omits ":" in one-line fields. Use this only for
        # specific labels; generic labels like "banco" often appear inside the
        # value itself (for example BANCO DE VENEZUELA).
        for alias in matching_aliases:
            if ":" not in raw_line and alias not in GENERIC_EXACT_LABELS:
                tail = normalized_line.split(alias, 1)[1].strip(" :.-")
                if tail:
                    candidates.append(tail)

        for offset in range(1, 4):
            next_idx = idx + offset
            if next_idx >= len(lines):
                break
            next_normalized = normalized_lines[next_idx]
            if is_label_header(next_normalized):
                break
            candidates.append(lines[next_idx])

        for candidate in candidates:
            candidate = scrub_candidate(candidate)
            if candidate:
                return candidate
    return None


def value_after_label_flexible(lines: list[str], aliases: list[str]) -> str | None:
    normalized_aliases = [norm(alias) for alias in aliases]
    normalized_lines = [norm(line) for line in lines]
    for idx, normalized_line in enumerate(normalized_lines):
        for alias in normalized_aliases:
            if not (
                normalized_line == alias
                or normalized_line.startswith(alias + ":")
                or normalized_line.startswith(alias + " ")
            ):
                continue

            raw_line = lines[idx]
            candidates: list[str] = []
            if ":" in raw_line:
                candidates.append(raw_line.split(":", 1)[1].strip())
            elif len(raw_line) > len(alias):
                candidates.append(raw_line[len(alias) :].strip())

            for offset in range(1, 4):
                next_idx = idx + offset
                if next_idx >= len(lines):
                    break
                next_normalized = normalized_lines[next_idx]
                if is_label_header(next_normalized):
                    break
                candidates.append(lines[next_idx])

            for candidate in candidates:
                candidate = scrub_candidate(candidate)
                if candidate:
                    return candidate
    return None


GENERIC_EXACT_LABELS = {
    "banco",
    "beneficiario",
    "concepto",
    "destino",
    "fecha",
    "identificacion",
    "monto",
    "numero celular",
    "operacion",
    "origen",
}


def line_matches_label(normalized_line: str, normalized_alias: str) -> bool:
    if normalized_alias in GENERIC_EXACT_LABELS:
        return normalized_line == normalized_alias or normalized_line.startswith(
            normalized_alias + ":"
        )
    return (
        normalized_line == normalized_alias
        or normalized_line.startswith(normalized_alias + ":")
        or normalized_line.startswith(normalized_alias + " ")
    )


def is_label_header(normalized_line: str) -> bool:
    return any(
        normalized_line == label or normalized_line.startswith(label + ":")
        for label in ALL_LABELS_NORMALIZED
    )


def scrub_candidate(value: str) -> str | None:
    value = value.strip()
    value = re.sub(r"^[=:\-.\s]+", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    # Ignore pure OCR icon noise.
    if not re.search(r"[A-Za-z0-9]", value):
        return None
    return value


LABEL_ALIASES: dict[str, list[str]] = {
    "reference": [
        "numero de referencia",
        "nro. de referencia",
        "nro de referencia",
        "referencia",
        "operacion",
    ],
    "amount": [
        "monto de la operacion",
        "monto (bs.)",
        "monto",
    ],
    "date": [
        "fecha y hora del envio",
        "fecha",
    ],
    "origin_phone": [
        "numero celular de origen",
        "celular de origen",
    ],
    "origin_account": [
        "cuenta origen",
        "origen",
    ],
    "recipient_phone": [
        "telf beneficiario",
        "numero celular de destino",
        "celular de destino",
        "numero celular",
        "destino",
    ],
    "recipient_id": [
        "ci /rif beneficiario",
        "cl /rif beneficiario",
        "rif beneficiario",
        "identificacion receptor",
        "identificacion",
        "documento de identidad",
    ],
    "origin_bank": [
        "banco emisor",
    ],
    "recipient_bank": [
        "banco receptor",
        "banco destino",
        "banco",
    ],
    "concept": [
        "concepto",
    ],
}

ALL_LABELS_NORMALIZED = {
    norm(label) for labels in LABEL_ALIASES.values() for label in labels
}


MONEY_RE = re.compile(
    r"(?<!\d)(?:Bs\.?\s*)?("
    r"[0-9]{1,3}(?:\.[0-9]{3})*,[0-9]{2}"
    r"|[0-9]+,[0-9]{2}"
    r"|[0-9]+\.[0-9]{2}"
    r")(?:\s*Bs\.?)?(?!\d)",
    re.IGNORECASE,
)


def extract_reference(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"\d{8,}", value)
    return match.group(0) if match else None


def extract_amount_from_value(value: str | None) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    match = MONEY_RE.search(value)
    if not match:
        return None, None
    raw = match.group(1)
    if "," in raw:
        normalized = raw.replace(".", "").replace(",", ".")
    else:
        normalized = raw
    try:
        decimal_value = Decimal(normalized)
    except InvalidOperation:
        return raw, None
    return raw, format(decimal_value, "f")


def extract_amount(lines: list[str], combined_text: str) -> tuple[str | None, str | None]:
    label_value = value_after_label(lines, LABEL_ALIASES["amount"])
    raw, normalized = extract_amount_from_value(label_value)
    if normalized:
        return raw, normalized

    for line in lines:
        raw, normalized = extract_amount_from_value(line)
        if normalized:
            return raw, normalized

    raw, normalized = extract_amount_from_value(combined_text)
    return raw, normalized


def normalize_phone(value: str | None) -> str | None:
    if not value:
        return None
    value = re.sub(r"[^0-9*]", "", value)
    return value or None


def normalize_document_id(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"([VEJGPCR]-?\s*)?([0-9][0-9.\s-]{4,})", value, re.IGNORECASE)
    if not match:
        return None
    prefix = (match.group(1) or "").upper().replace(" ", "").replace("-", "")
    number = re.sub(r"\D", "", match.group(2))
    return f"{prefix}-{number}" if prefix else number


def normalize_bank(value: str | None) -> str | None:
    if not value:
        return None
    value = re.sub(r"\s+", " ", value).strip(" .")
    value = value.replace("S.a.c.a.", "S.A.C.A.").replace("s.a.c.a.", "S.A.C.A.")
    return value.upper() if value else None


def normalize_concept(value: str | None) -> str | None:
    if not value:
        return None
    value = re.sub(r"[^A-Za-z0-9 /._-]", "", strip_accents(value)).strip()
    return value.upper() if value else None


def parse_date_time(raw_value: str | None) -> dict[str, str | None]:
    if not raw_value:
        return {"raw": None, "iso": None}

    raw = raw_value.strip()
    cleaned = strip_accents(raw)
    cleaned = re.sub(r"\ba\s+las\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    date_match = re.search(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", cleaned)
    if not date_match:
        return {"raw": raw, "iso": None}

    day, month, year = (int(part) for part in date_match.groups())
    if year < 100:
        year += 2000

    time_match = re.search(
        r"(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([AP]\.?M\.?)?",
        cleaned,
        flags=re.IGNORECASE,
    )

    try:
        if not time_match:
            parsed_date = date(year, month, day)
            return {"raw": raw, "iso": parsed_date.isoformat()}

        hour = int(time_match.group(1))
        minute = int(time_match.group(2))
        second = int(time_match.group(3) or 0)
        am_pm = (time_match.group(4) or "").replace(".", "").upper()
        if am_pm == "PM" and hour < 12:
            hour += 12
        if am_pm == "AM" and hour == 12:
            hour = 0
        parsed = datetime(year, month, day, hour, minute, second)
    except ValueError:
        return {"raw": raw, "iso": None}

    return {"raw": raw, "iso": parsed.isoformat(timespec="seconds")}


def ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def windows_threshold_crop(
    image_path: Path,
    output_path: Path,
    *,
    rect_pct: tuple[float, float, float, float],
    threshold: int = 170,
    scale: int = 3,
) -> bool:
    """Create a black-on-white cropped PNG using PowerShell/System.Drawing."""

    if os.name != "nt":
        return False
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        return False

    x_pct, y_pct, w_pct, h_pct = rect_pct
    script = f"""
Add-Type -AssemblyName System.Drawing
$src = {ps_quote(str(image_path))}
$dst = {ps_quote(str(output_path))}
$xPct = [double]{x_pct}
$yPct = [double]{y_pct}
$wPct = [double]{w_pct}
$hPct = [double]{h_pct}
$threshold = [double]{threshold}
$scale = [int]{scale}
$img = [System.Drawing.Bitmap]::FromFile($src)
try {{
  $x = [Math]::Max(0, [int][Math]::Round($img.Width * $xPct))
  $y = [Math]::Max(0, [int][Math]::Round($img.Height * $yPct))
  $w = [Math]::Min($img.Width - $x, [int][Math]::Round($img.Width * $wPct))
  $h = [Math]::Min($img.Height - $y, [int][Math]::Round($img.Height * $hPct))
  $rect = [Drawing.Rectangle]::new($x, $y, $w, $h)
  $crop = $img.Clone($rect, $img.PixelFormat)
  try {{
    $out = [Drawing.Bitmap]::new($crop.Width * $scale, $crop.Height * $scale)
    try {{
      for ($yy = 0; $yy -lt $out.Height; $yy++) {{
        for ($xx = 0; $xx -lt $out.Width; $xx++) {{
          $sx = [int][Math]::Floor($xx / $scale)
          $sy = [int][Math]::Floor($yy / $scale)
          $p = $crop.GetPixel($sx, $sy)
          $bright = ($p.R + $p.G + $p.B) / 3
          if ($bright -gt $threshold) {{
            $color = [Drawing.Color]::Black
          }} else {{
            $color = [Drawing.Color]::White
          }}
          $out.SetPixel($xx, $yy, $color)
        }}
      }}
      $out.Save($dst, [System.Drawing.Imaging.ImageFormat]::Png)
    }} finally {{
      $out.Dispose()
    }}
  }} finally {{
    $crop.Dispose()
  }}
}} finally {{
  $img.Dispose()
}}
"""

    proc = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return proc.returncode == 0 and output_path.exists()


def pillow_threshold_crop(
    image_path: Path,
    output_path: Path,
    *,
    rect_pct: tuple[float, float, float, float],
    threshold: int = 170,
    scale: int = 3,
) -> bool:
    """Create a black-on-white cropped PNG using Pillow when available."""

    try:
        from PIL import Image
    except ImportError:
        return False

    try:
        with Image.open(image_path) as image:
            image = image.convert("RGB")
            width, height = image.size
            x_pct, y_pct, w_pct, h_pct = rect_pct
            x = max(0, round(width * x_pct))
            y = max(0, round(height * y_pct))
            crop_width = min(width - x, round(width * w_pct))
            crop_height = min(height - y, round(height * h_pct))
            if crop_width <= 0 or crop_height <= 0:
                return False

            crop = image.crop((x, y, x + crop_width, y + crop_height))
            if scale > 1:
                resample = getattr(getattr(Image, "Resampling", Image), "NEAREST")
                crop = crop.resize((crop.width * scale, crop.height * scale), resample)

            grayscale = crop.convert("L")
            thresholded = grayscale.point(
                lambda pixel: 0 if pixel > threshold else 255,
                mode="1",
            )
            thresholded.save(output_path, "PNG")
    except Exception:
        return False

    return output_path.exists()


def mercantil_recovery_needed(current_text: str) -> bool:
    """Whether the required fields are still missing after the primary pass.

    Mercantil's Tpago success screen has a gradient banner with a curved
    white-card cutout underneath it. That gradient/curve combination
    confuses Tesseract's default Otsu thresholding, which can drop entire
    label/value lines on the card (not just the banner) from the primary
    OCR pass, well beyond just the amount field BDV struggles with.
    """

    lines = clean_lines(current_text)
    reference = extract_reference(value_after_label(lines, LABEL_ALIASES["reference"]))
    _, amount_value = extract_amount(lines, current_text)
    date_time = parse_date_time(value_after_label(lines, LABEL_ALIASES["date"]))
    concept = normalize_concept(value_after_label(lines, LABEL_ALIASES["concept"]))
    recipient_bank = normalize_bank(value_after_label(lines, LABEL_ALIASES["recipient_bank"]))
    return not all([reference, amount_value, date_time["raw"], concept, recipient_bank])


def targeted_ocr_passes(
    image_path: Path,
    tesseract_path: str,
    bank_app: str | None,
    current_text: str,
    *,
    lang: str = DEFAULT_LANG,
    enable_crops: bool = True,
) -> list[dict[str, str]]:
    passes: list[dict[str, str]] = []
    if not enable_crops:
        return passes

    amount_raw, amount_value = extract_amount(clean_lines(current_text), current_text)
    should_try_bdv_amount = (
        bank_app == "bdv" or has_bdv_receipt_signal(current_text)
    ) and not amount_value

    if should_try_bdv_amount:
        temp_file = tempfile.NamedTemporaryFile(
            prefix="receipt_ocr_bdv_",
            suffix=".png",
            delete=False,
        )
        crop_path = Path(temp_file.name)
        temp_file.close()
        try:
            # BDV places the amount in a centered grey bar near the top-middle.
            # Percentages make the crop work across screenshots with the same UI.
            crop_options = {
                "rect_pct": (0.055, 0.286, 0.89, 0.08),
                "threshold": 170,
                "scale": 3,
            }
            created = pillow_threshold_crop(
                image_path,
                crop_path,
                **crop_options,
            ) or windows_threshold_crop(image_path, crop_path, **crop_options)
            if created:
                try:
                    crop_text = run_tesseract(crop_path, tesseract_path, lang=lang, psm=7)
                except RuntimeError:
                    crop_text = ""
                if crop_text.strip():
                    passes.append({"name": "bdv_amount_crop", "text": crop_text.strip()})
        finally:
            try:
                crop_path.unlink(missing_ok=True)
            except OSError:
                pass

    # When Tesseract drops most label lines from a Mercantil receipt, it can
    # also take the "tpago"/"mercantil" tokens with it, so bank detection
    # falls through to the loose "banco de venezuela" substring match meant
    # for the *destination* bank and misreports "bdv" instead of "mercantil".
    # That fallback match only fires when the real BDV app signal
    # (has_bdv_receipt_signal) is absent, so treat that specific case as
    # ambiguous rather than a confident BDV detection.
    bank_is_ambiguous = bank_app is None or (
        bank_app == "bdv" and not has_bdv_receipt_signal(current_text)
    )
    should_try_mercantil_recovery = (
        bank_app == "mercantil"
        or has_any_token(current_text, MERCANTIL_RECEIPT_TOKENS)
        or bank_is_ambiguous
    ) and mercantil_recovery_needed(current_text)

    if should_try_mercantil_recovery:
        temp_file = tempfile.NamedTemporaryFile(
            prefix="receipt_ocr_mercantil_",
            suffix=".png",
            delete=False,
        )
        crop_path = Path(temp_file.name)
        temp_file.close()
        try:
            # Unlike BDV's single narrow field, the Mercantil miss can span
            # most of the card, so threshold the whole page instead of a
            # small crop. Scale is left at 1: the fix is binarizing away the
            # gradient, not upscaling, and it keeps the Windows PowerShell/
            # .NET per-pixel fallback fast on a full-size screenshot.
            crop_options = {
                "rect_pct": (0.0, 0.0, 1.0, 1.0),
                "threshold": 170,
                "scale": 1,
            }
            created = pillow_threshold_crop(
                image_path,
                crop_path,
                **crop_options,
            ) or windows_threshold_crop(image_path, crop_path, **crop_options)
            if created:
                try:
                    crop_text = run_tesseract(crop_path, tesseract_path, lang=lang, psm=6)
                except RuntimeError:
                    crop_text = ""
                if crop_text.strip():
                    passes.append(
                        {"name": "mercantil_threshold_pass", "text": crop_text.strip()}
                    )
        finally:
            try:
                crop_path.unlink(missing_ok=True)
            except OSError:
                pass

    return passes


def transaction_key(receipt: dict[str, Any]) -> str | None:
    parts = [
        receipt["payment"].get("bank_app") or "",
        receipt["payment"].get("reference") or "",
        receipt["payment"]["amount"].get("value") or "",
        receipt["payment"]["date_time"].get("iso") or receipt["payment"]["date_time"].get("raw") or "",
        receipt["recipient"].get("phone") or receipt["recipient"].get("document_id") or "",
    ]
    if not any(parts[1:]):
        return None
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def parse_receipt_image(image_path: Path, options: ReceiptOptions) -> dict[str, Any]:
    """Parse one receipt image into the normalized VEPay receipt model."""

    image_path = Path(image_path)
    tesseract_path = options.tesseract_path
    lang = options.lang
    include_raw_text = options.include_raw_text
    enable_crops = options.enable_crops

    primary_text = run_tesseract(image_path, tesseract_path, lang=lang, psm=6)
    primary_bank = detect_bank(primary_text)
    extra_passes = targeted_ocr_passes(
        image_path,
        tesseract_path,
        primary_bank,
        primary_text,
        lang=lang,
        enable_crops=enable_crops,
    )

    combined_text = primary_text
    for extra in extra_passes:
        combined_text += "\n" + extra["text"]

    lines = clean_lines(combined_text)
    bank_app = detect_bank(combined_text)

    reference = extract_reference(value_after_label(lines, LABEL_ALIASES["reference"]))
    amount_raw, amount_value = extract_amount(lines, combined_text)
    date_time = parse_date_time(value_after_label(lines, LABEL_ALIASES["date"]))

    origin_phone = normalize_phone(value_after_label(lines, LABEL_ALIASES["origin_phone"]))
    origin_account = value_after_label(lines, LABEL_ALIASES["origin_account"])
    recipient_phone = normalize_phone(value_after_label(lines, LABEL_ALIASES["recipient_phone"]))
    recipient_id = normalize_document_id(value_after_label(lines, LABEL_ALIASES["recipient_id"]))
    origin_bank = normalize_bank(value_after_label(lines, LABEL_ALIASES["origin_bank"]))
    recipient_bank = normalize_bank(value_after_label(lines, LABEL_ALIASES["recipient_bank"]))
    bank_app = detect_bank_from_name(origin_bank) or bank_app
    concept = normalize_concept(value_after_label(lines, LABEL_ALIASES["concept"]))

    # Bank-specific cleanup where labels share words (for example "Banco" can
    # refer to recipient bank in BDV/Bancamiga but Banesco exposes both banks).
    if bank_app == "banesco":
        recipient_phone = normalize_phone(
            value_after_label(lines, ["numero celular de destino", "celular de destino"])
        )
    if bank_app == "mercantil":
        origin_phone = None
        recipient_phone = normalize_phone(value_after_label(lines, ["beneficiario"]))
        if origin_account:
            origin_account = origin_account.strip()
    if bank_app == "provincial":
        reference = extract_reference(
            value_after_label_flexible(lines, ["referencia"])
        ) or reference
        provincial_date = parse_date_time(value_after_label_flexible(lines, ["fecha"]))
        if provincial_date["raw"]:
            date_time = provincial_date
        recipient_phone = normalize_phone(
            value_after_label_flexible(lines, ["numero celular"])
        ) or recipient_phone
        recipient_id = normalize_document_id(
            value_after_label_flexible(lines, ["identificacion"])
        ) or recipient_id
        recipient_bank = normalize_bank(
            value_after_label_flexible(lines, ["banco"])
        ) or recipient_bank
        concept = normalize_concept(
            value_after_label_flexible(lines, ["concepto"])
        ) or concept

    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "source": {
            "file_name": image_path.name,
            "file_path": str(image_path.resolve()),
            "sha256": source_sha256(image_path),
        },
        "payment": {
            "bank_app": bank_app,
            "status": detect_status(combined_text),
            "reference": reference,
            "amount": {
                "value": amount_value,
                "currency": "VES",
                "raw": amount_raw,
            },
            "date_time": date_time,
            "concept": concept,
        },
        "origin": {
            "phone": origin_phone,
            "account": origin_account,
            "bank": origin_bank,
        },
        "recipient": {
            "phone": recipient_phone,
            "document_id": recipient_id,
            "bank": recipient_bank,
        },
        "ocr": {
            "engine": "tesseract",
            "language": lang,
            "passes": ["full_psm_6"] + [extra["name"] for extra in extra_passes],
        },
        "validation": {
            "is_complete": False,
            "missing_fields": [],
            "warnings": [],
        },
    }

    if include_raw_text:
        receipt["ocr"]["raw_text"] = combined_text

    receipt["transaction_key"] = transaction_key(receipt)
    missing = missing_fields(receipt)
    receipt["validation"]["missing_fields"] = missing
    receipt["validation"]["is_complete"] = not missing

    if bank_app is None:
        receipt["validation"]["warnings"].append("No se pudo detectar el banco/app.")
    if not extra_passes and bank_app == "bdv" and not amount_value:
        receipt["validation"]["warnings"].append(
            "No se pudo leer el monto BDV; intenta instalar Pillow/OpenCV o revisar manualmente."
        )
    if bank_app == "provincial" and (
        "payment.reference" in missing or "recipient.bank" in missing
    ):
        receipt["validation"]["warnings"].append(
            "Provincial alternate layout does not expose all confirmation fields; manual review required."
        )

    return receipt


def build_receipt(
    image_path: Path,
    *,
    tesseract_path: str,
    lang: str = DEFAULT_LANG,
    include_raw_text: bool = True,
    enable_windows_crops: bool | None = None,
    enable_crops: bool | None = None,
) -> dict[str, Any]:
    """Backward-compatible wrapper used by the original CLI and integrations."""

    if enable_crops is None:
        enable_crops = True if enable_windows_crops is None else enable_windows_crops
    options = ReceiptOptions(
        tesseract_path=tesseract_path,
        lang=lang,
        include_raw_text=include_raw_text,
        enable_crops=enable_crops,
    )
    return parse_receipt_image(image_path, options)


def get_nested(data: dict[str, Any], dotted_path: str) -> Any:
    current: Any = data
    for part in dotted_path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def missing_fields(receipt: dict[str, Any]) -> list[str]:
    missing = []
    for path in REQUIRED_EXTRACTION_FIELDS:
        value = get_nested(receipt, path)
        if value in (None, ""):
            missing.append(path)
    return missing


def discover_images(paths: list[str]) -> list[Path]:
    images: list[Path] = []
    for raw_path in paths:
        path = Path(raw_path)
        if path.is_dir():
            images.extend(
                sorted(
                    item
                    for item in path.iterdir()
                    if item.is_file() and item.suffix.lower() in IMAGE_EXTENSIONS
                )
            )
        elif path.is_file():
            images.append(path)
        else:
            raise FileNotFoundError(f"No existe: {raw_path}")
    return images


def flatten_receipt(receipt: dict[str, Any]) -> dict[str, str]:
    return {
        "schema_version": receipt["schema_version"],
        "source_file": receipt["source"]["file_name"],
        "source_path": receipt["source"]["file_path"],
        "source_sha256": receipt["source"]["sha256"],
        "bank_app": receipt["payment"].get("bank_app") or "",
        "status": receipt["payment"].get("status") or "",
        "reference": receipt["payment"].get("reference") or "",
        "amount_value": receipt["payment"]["amount"].get("value") or "",
        "amount_currency": receipt["payment"]["amount"].get("currency") or "",
        "amount_raw": receipt["payment"]["amount"].get("raw") or "",
        "date_time_iso": receipt["payment"]["date_time"].get("iso") or "",
        "date_time_raw": receipt["payment"]["date_time"].get("raw") or "",
        "concept": receipt["payment"].get("concept") or "",
        "origin_phone": receipt["origin"].get("phone") or "",
        "origin_account": receipt["origin"].get("account") or "",
        "origin_bank": receipt["origin"].get("bank") or "",
        "recipient_phone": receipt["recipient"].get("phone") or "",
        "recipient_id": receipt["recipient"].get("document_id") or "",
        "recipient_bank": receipt["recipient"].get("bank") or "",
        "transaction_key": receipt.get("transaction_key") or "",
        "is_complete": str(receipt["validation"]["is_complete"]).lower(),
        "missing_fields": ",".join(receipt["validation"]["missing_fields"]),
        "warnings": " | ".join(receipt["validation"]["warnings"]),
    }


def write_output(receipts: list[dict[str, Any]], output_format: str, output: str | None) -> None:
    if output_format == "json":
        content = json.dumps(receipts, ensure_ascii=False, indent=2)
        if output:
            Path(output).write_text(content + "\n", encoding="utf-8")
        else:
            print(content)
        return

    if output_format == "jsonl":
        content = "\n".join(json.dumps(item, ensure_ascii=False) for item in receipts)
        if output:
            Path(output).write_text(content + "\n", encoding="utf-8")
        else:
            print(content)
        return

    if output_format == "csv":
        rows = [flatten_receipt(receipt) for receipt in receipts]
        fieldnames = list(rows[0].keys()) if rows else list(flatten_receipt(empty_receipt()).keys())
        if output:
            with Path(output).open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
        else:
            writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        return

    raise ValueError(f"Formato no soportado: {output_format}")


def empty_receipt() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "source": {"file_name": "", "file_path": "", "sha256": ""},
        "payment": {
            "bank_app": "",
            "status": "",
            "reference": "",
            "amount": {"value": "", "currency": "VES", "raw": ""},
            "date_time": {"iso": "", "raw": ""},
            "concept": "",
        },
        "origin": {"phone": "", "account": "", "bank": ""},
        "recipient": {"phone": "", "document_id": "", "bank": ""},
        "transaction_key": "",
        "validation": {"is_complete": False, "missing_fields": [], "warnings": []},
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "VEPay API extrae datos de pago desde capturas de Bancamiga, "
            "Banesco, BDV, Mercantil y Provincial."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """
            Ejemplos:
              python vepay_api_core.py "C:\\pagos\\bancamiga.jpeg"
              python vepay_api_core.py "C:\\pagos" --format jsonl --output pagos.jsonl
              python vepay_api_core.py *.jpeg --format csv --output pagos.csv
            """
        ).strip(),
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {APP_VERSION}")
    parser.add_argument("images", nargs="+", help="Imagen(es) o carpeta(s) con capturas.")
    parser.add_argument("--output", "-o", help="Archivo de salida. Si se omite, imprime en consola.")
    parser.add_argument(
        "--format",
        choices=("json", "jsonl", "csv"),
        default="json",
        help="Formato de salida.",
    )
    parser.add_argument("--lang", default=DEFAULT_LANG, help="Idiomas Tesseract, por defecto spa+eng.")
    parser.add_argument("--tesseract", help="Ruta a tesseract.exe si no esta en PATH.")
    parser.add_argument(
        "--no-raw-text",
        action="store_true",
        help="No incluir texto OCR crudo en la salida.",
    )
    parser.add_argument(
        "--no-crops",
        dest="no_crops",
        action="store_true",
        help="Desactiva recortes auxiliares para mejorar campos dificiles como monto BDV.",
    )
    parser.add_argument(
        "--no-windows-crops",
        dest="no_crops",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        tesseract_path = find_tesseract(args.tesseract)
        images = discover_images(args.images)
        if not images:
            raise RuntimeError("No se encontraron imagenes para procesar.")

        receipts = [
            build_receipt(
                image,
                tesseract_path=tesseract_path,
                lang=args.lang,
                include_raw_text=not args.no_raw_text,
                enable_crops=not args.no_crops,
            )
            for image in images
        ]
        write_output(receipts, args.format, args.output)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
