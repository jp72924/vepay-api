FROM python:3.12.13-slim-bookworm

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
COPY vepay_api_core.py vepay_api.py ./
COPY client ./client
COPY examples ./examples
COPY schemas ./schemas

RUN python -m pip install --no-cache-dir . \
    && useradd --create-home --shell /usr/sbin/nologin vepay

USER vepay

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)"]

CMD ["vepay-api"]
