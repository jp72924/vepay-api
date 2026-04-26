# VEPay OCR

VEPay OCR is a local command-line tool that extracts structured payment data
from Venezuelan mobile banking receipt screenshots.

It currently supports receipt layouts from Bancamiga, Banesco, Banco de
Venezuela (BDV), Mercantil Tpago and BBVA Provincial Dinero Rapido. The output
is normalized as JSON, JSONL or CSV so it can be stored, reviewed, or sent to
another system for transaction confirmation.

## Features

- Local OCR workflow using Tesseract.
- Bank-specific parsing for common Venezuelan mobile payment receipts.
- Standard JSON model for reference, amount, date/time, concept, bank and
  recipient data.
- CSV and JSONL exports for spreadsheets, queues and ingestion pipelines.
- Deterministic `transaction_key` for duplicate detection.
- Validation metadata for manual review when a required field is missing.
- Partial extraction for receipt layouts that do not expose all confirmation
  fields.

## Requirements

- Python 3.10 or newer.
- Tesseract OCR installed and available in `PATH`.
- Tesseract language data for `spa` and `eng`.

On Windows, VEPay OCR can use PowerShell/.NET for one targeted BDV crop because
that app often renders the amount as white text on a grey bar.

## Installation

Run directly with Python:

```powershell
python vepayocr.py --version
```

Or install the local project to expose the `vepayocr` command:

```powershell
python -m pip install .
vepayocr --version
```

## Usage

Process one image:

```powershell
python vepayocr.py ".\capturas\bancamiga.jpeg"
```

Process a folder and write JSON:

```powershell
python vepayocr.py ".\capturas" --format json --output pagos.json
```

Write JSONL for incremental ingestion:

```powershell
python vepayocr.py ".\capturas" --format jsonl --output pagos.jsonl
```

Write CSV for review:

```powershell
python vepayocr.py ".\capturas" --format csv --output pagos.csv --no-raw-text
```

Use a custom Tesseract executable:

```powershell
python vepayocr.py ".\capturas" --tesseract "C:\Program Files\Tesseract-OCR\tesseract.exe"
```

Show the installed tool version:

```powershell
python vepayocr.py --version
```

## Output Model

The JSON output follows `payment_receipt_schema.json`.

- `schema_version`: currently `ve_bank_payment_receipt_v1`.
- `source`: file name, path and SHA-256 hash of the processed image.
- `payment`: detected bank/app, status, reference, amount, date/time and concept.
- `origin`: source phone, account and bank when present in the screenshot.
- `recipient`: recipient phone, document ID and bank.
- `ocr`: engine, language, OCR passes and optional raw OCR text.
- `transaction_key`: deterministic hash for duplicate detection.
- `validation`: completeness flag, missing fields and warnings.

Required fields for a complete receipt:

- `payment.reference`
- `payment.amount.value`
- `payment.date_time.raw`
- `recipient.bank`
- `payment.concept`

Amounts are normalized to decimal strings with a dot separator, for example
`2.500,00` becomes `2500.00`. Currency is currently fixed as `VES`.

Some BBVA Provincial screenshots do not show a payment reference or destination
bank. VEPay OCR still extracts the visible fields from those layouts, but marks
the receipt as incomplete and adds a warning for manual review.

## Privacy

Do not commit real bank screenshots, OCR outputs, phone numbers, document IDs or
payment references. The repository is configured to ignore common local OCR
outputs and private sample files.

Use `examples/redacted_receipt.json` as the public reference shape for the
output model.

## License

Apache License 2.0. See `LICENSE`.
