FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VEPAY_API_HOST=0.0.0.0 \
    VEPAY_API_PORT=8080

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-spa \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md LICENSE NOTICE ./
COPY vepay_api_core.py vepay_api.py vepayocr.py vepayocr_api.py payment_receipt_schema.json ./
COPY examples ./examples

RUN python -m pip install --no-cache-dir .

EXPOSE 8080

CMD ["uvicorn", "vepay_api:app", "--host", "0.0.0.0", "--port", "8080"]
