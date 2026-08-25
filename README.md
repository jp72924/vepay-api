# VEPay API

VEPay API extracts structured payment data from Venezuelan mobile banking
receipt screenshots. It can run as a local command-line tool or as a small HTTP
API for integrations over the network.

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
- FastAPI HTTP service with multipart and JSON/base64 inputs.
- Optional same-origin audit UI served at `/ui`.
- In-memory async jobs for larger batches.
- Docker image recipe with Tesseract, Spanish and English OCR data.

## Recent Changes

This release improves parser accuracy and adds a browser-based audit workflow
without changing the API-first default behavior.

- Parser accuracy: receipt classification now prioritizes app and emitter-bank
  context over destination-bank text, so BDV, Banesco, Bancamiga, Mercantil
  Tpago and BBVA Provincial receipts are less likely to be confused by
  counterparty banks.
- Amount extraction: BDV amount crops are triggered from strong BDV receipt
  signals, and amounts with comma or dot decimals followed by `Bs` are
  normalized to the same JSON shape.
- Mercantil Tpago recovery: when a Mercantil receipt (or one Tesseract
  couldn't confidently classify) comes back missing required fields, a
  full-page thresholded re-OCR pass recovers the label/value lines its
  gradient banner otherwise causes Tesseract to drop.
- Integrated audit UI: the static client can be served from `/ui` when
  `VEPAY_API_ENABLE_UI=true`, with optional `/` to `/ui/` redirection via
  `VEPAY_API_UI_DEFAULT_ROUTE=true`.
- Local proxy client: `scripts/start_client.py` still serves the same UI on
  `127.0.0.1:8765` and proxies `/api/*` to a configurable upstream API via
  `VEPAY_API_BASE_URL` for CORS-free manual audits.
- Deployment and validation: Docker now includes the `client/` assets, Compose
  keeps the UI disabled by default, and tests cover contextual bank detection,
  BDV amount fallback, Banesco destination phone parsing, UI routing, asset
  path safety and local proxy behavior.

## Requirements

- Python 3.10 or newer.
- Tesseract OCR installed and available in `PATH`.
- Tesseract language data for `spa` and `eng`.

VEPay API uses Pillow for a targeted BDV crop because that app often renders
the amount as white text on a grey bar, and for a full-page thresholding pass
on Mercantil Tpago receipts, whose gradient banner and curved white-card
cutout can otherwise confuse Tesseract's default thresholding badly enough to
drop most of the card's fields. On Windows, the legacy PowerShell/.NET crop
remains available as a fallback for both passes.

## Installation

Run directly with Python:

```powershell
python vepay_api_core.py --version
```

Or install the local project to expose the `vepay-api` API server and
`vepay-api-cli` CLI commands:

```powershell
python -m pip install .
vepay-api-cli --version
```

Install development dependencies for tests:

```powershell
python -m pip install ".[dev]"
```

## Usage

Process one image:

```powershell
python vepay_api_core.py ".\capturas\bancamiga.jpeg"
```

Process a folder and write JSON:

```powershell
python vepay_api_core.py ".\capturas" --format json --output pagos.json
```

Write JSONL for incremental ingestion:

```powershell
python vepay_api_core.py ".\capturas" --format jsonl --output pagos.jsonl
```

Write CSV for review:

```powershell
python vepay_api_core.py ".\capturas" --format csv --output pagos.csv --no-raw-text
```

Use a custom Tesseract executable:

```powershell
python vepay_api_core.py ".\capturas" --tesseract "C:\Program Files\Tesseract-OCR\tesseract.exe"
```

Disable auxiliary OCR crops:

```powershell
python vepay_api_core.py ".\capturas" --no-crops
```

Show the installed tool version:

```powershell
python vepay_api_core.py --version
```

## HTTP API

Start the API locally:

```powershell
python -m uvicorn vepay_api:app --host 127.0.0.1 --port 8080
```

Or start it in the background with PID/log files:

```powershell
python scripts/start_api.py
python scripts/stop_api.py
```

Run a finite smoke test that starts the API, checks it and stops it:

```powershell
python scripts/smoke_api.py
```

If the `vepay-api` console script directory is on your `PATH`, this also
works:

```powershell
vepay-api
```

Health and capabilities:

```powershell
curl http://localhost:8080/
curl http://localhost:8080/health
curl http://localhost:8080/v1/capabilities
```

`/healthz` is also available and returns the identical response as `/health` — a
Kubernetes-style alias for tooling that defaults to that convention.

For a browser UI, open `http://localhost:8080/docs`.

Parse one or more images with multipart form data:

```powershell
curl -X POST http://localhost:8080/v1/receipts/parse `
  -F "files=@.\capturas\bdv.jpeg" `
  -F "include_raw_text=false" `
  -F "enable_crops=true"
```

Python client:

```python
import requests

with open("capturas/bdv.jpeg", "rb") as handle:
    response = requests.post(
        "http://localhost:8080/v1/receipts/parse",
        files={"files": ("bdv.jpeg", handle, "image/jpeg")},
        data={"include_raw_text": "false", "enable_crops": "true"},
        timeout=60,
    )
response.raise_for_status()
print(response.json())
```

JavaScript client:

```javascript
const form = new FormData();
form.append("files", fileInput.files[0], "bdv.jpeg");
form.append("include_raw_text", "false");
form.append("enable_crops", "true");

const response = await fetch("http://localhost:8080/v1/receipts/parse", {
  method: "POST",
  body: form,
});
console.log(await response.json());
```

Parse base64 JSON when multipart is not convenient:

```json
{
  "images": [
    {
      "filename": "bdv.jpeg",
      "content_type": "image/jpeg",
      "image_base64": "<base64>"
    }
  ],
  "lang": "spa+eng",
  "include_raw_text": false,
  "enable_crops": true
}
```

```powershell
curl -X POST http://localhost:8080/v1/receipts/parse-json `
  -H "Content-Type: application/json" `
  --data-binary "@request.json"
```

Create an asynchronous in-memory job with the same JSON shape:

```powershell
curl -X POST http://localhost:8080/v1/jobs `
  -H "Content-Type: application/json" `
  --data-binary "@request.json"

curl http://localhost:8080/v1/jobs/<job_id>
```

Jobs are an MVP single-instance queue stored in process memory. For multiple API
instances or durable processing, replace the in-memory job store with Redis/RQ
or another external queue.

The API response includes:

- `request_id`
- `schema_version`
- `receipts`
- `summary`
- `errors`

For privacy, API responses replace local server paths with
`upload://{request_id}/{filename}` and omit OCR `raw_text` unless
`include_raw_text=true`.

## Local Audit Client

VEPay API includes a small local browser client for manually sending receipt
images to the deployed API and reviewing the structured response.

The same client can also be served by the API itself. It is disabled by default
so API-only deployments keep the same public surface:

```powershell
$env:VEPAY_API_ENABLE_UI="true"
python -m uvicorn vepay_api:app --host 127.0.0.1 --port 8080
```

Open:

```text
http://127.0.0.1:8080/ui
```

When served from `/ui`, the browser calls the same-origin `/v1/*` endpoints and
does not need CORS or the local proxy. If `VEPAY_API_REQUIRE_API_KEY=true`, the
UI shows a temporary `X-API-Key` field and does not persist it.

For local audit sessions where the UI should be the first screen, also set
`VEPAY_API_UI_DEFAULT_ROUTE=true`; then `GET /` redirects to `/ui/` while the
API endpoints remain available.

Start the local client:

```powershell
python scripts/start_client.py
```

Open:

```text
http://127.0.0.1:8765
```

The client proxies `/api/*` requests to `http://127.0.0.1:8080/` by default. To
point it at a different API instance instead:

```powershell
$env:VEPAY_API_BASE_URL="http://127.0.0.1:8080"
python scripts/start_client.py
```

In the UI, select or drop receipt screenshots, process them, review the summary,
receipt cards, per-file errors and full JSON, then download the response for
manual audit.

## Local OCR Regression Checklist

Private receipt screenshots should stay outside the repository. When validating
local parser changes with the current manual fixture set, run:

```powershell
$samples = @(
  "C:\Users\Workstation\Desktop\RetailOps App\bancamiga.jpeg",
  "C:\Users\Workstation\Desktop\RetailOps App\banesco.jpeg",
  "C:\Users\Workstation\Desktop\RetailOps App\banesco-test.jpeg",
  "C:\Users\Workstation\Desktop\RetailOps App\bdv.jpeg",
  "C:\Users\Workstation\Desktop\RetailOps App\bdv-test.jpeg",
  "C:\Users\Workstation\Desktop\RetailOps App\mercantil.jpeg",
  "C:\Users\Workstation\Desktop\RetailOps App\provincial.jpeg",
  "C:\Users\Workstation\Desktop\RetailOps App\provincial_alt.jpeg"
)
python vepay_api_core.py $samples --format json
```

Expected `payment.bank_app` values:

- `bancamiga.jpeg`: `bancamiga`
- `banesco.jpeg`: `banesco`
- `banesco-test.jpeg`: `banesco`
- `bdv.jpeg`: `bdv`
- `bdv-test.jpeg`: `bdv`
- `mercantil.jpeg`: `mercantil`
- `provincial.jpeg`: `provincial`
- `provincial_alt.jpeg`: `provincial`

Optional environment variables:

- `VEPAY_API_TESSERACT`: custom Tesseract executable path.
- `VEPAY_API_MAX_FILES`: max synchronous files, default `10`.
- `VEPAY_API_MAX_JOB_FILES`: max job files, default `100`.
- `VEPAY_API_MAX_FILE_SIZE_BYTES`: per-image limit, default `10485760`.
- `VEPAY_API_MAX_CONCURRENCY`: concurrent Tesseract runs, default `min(4, cpu_count)`.
- `VEPAY_API_JOB_TTL_SECONDS`: in-memory job lifetime, default `86400`.
- `VEPAY_API_ENABLE_UI`: set `true` to serve the audit UI at `/ui`.
- `VEPAY_API_UI_DEFAULT_ROUTE`: set `true` with UI enabled to redirect `/` to `/ui/`.
- `VEPAY_API_REQUIRE_API_KEY`: set `true` to require `X-API-Key`.
- `VEPAY_API_KEY`: expected API key when auth is enabled.

Run with Docker:

```powershell
docker build -t vepay-api .
docker run --rm -p 8080:8080 vepay-api
```

Expose the integrated audit UI in Docker:

```powershell
docker run --rm -p 8080:8080 -e VEPAY_API_ENABLE_UI=true vepay-api
```

Or with Compose:

```powershell
docker compose up --build
```

## Output Model

The JSON output follows `schemas/payment_receipt_schema.json`.

- `schema_version`: currently `vepay_api_receipt_v1`.
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
bank. VEPay API still extracts the visible fields from those layouts, but marks
the receipt as incomplete and adds a warning for manual review.

## Privacy

Do not commit real bank screenshots, OCR outputs, phone numbers, document IDs or
payment references. The repository is configured to ignore common local OCR
outputs and private sample files.

Use `examples/redacted_receipt.json` as the public reference shape for the
output model.

## License

Apache License 2.0. See `LICENSE`.

VEPay API is derived from the original VEPay OCR utility and keeps attribution
in `NOTICE`.

