"""HTTP API for VEPay API.

The API keeps uploaded images in temporary directories, returns normalized JSON,
and replaces local file paths with upload-scoped URIs before responding.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.responses import FileResponse, RedirectResponse, Response

from vepay_api_core import (
    APP_NAME,
    APP_VERSION,
    DEFAULT_LANG,
    IMAGE_EXTENSIONS,
    SCHEMA_VERSION,
    ReceiptOptions,
    find_tesseract,
    parse_receipt_image,
)


SUPPORTED_BANKS = ("bancamiga", "banesco", "bdv", "mercantil", "provincial")
ALLOWED_CONTENT_TYPES = {
    "application/octet-stream",
    "image/bmp",
    "image/jpeg",
    "image/png",
    "image/tiff",
    "image/webp",
    "image/x-bmp",
    "image/x-ms-bmp",
}
CHUNK_SIZE = 1024 * 1024
ROOT_DIR = Path(__file__).resolve().parent
CLIENT_DIR = ROOT_DIR / "client"


def env_value(name: str) -> str | None:
    return os.getenv(name)


def int_env(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        return max(minimum, int(env_value(name) or str(default)))
    except ValueError:
        return default


def bool_env(name: str, default: bool = False) -> bool:
    raw = env_value(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


MAX_FILES = int_env("VEPAY_API_MAX_FILES", 10)
MAX_JOB_FILES = int_env("VEPAY_API_MAX_JOB_FILES", 100)
MAX_FILE_SIZE_BYTES = int_env("VEPAY_API_MAX_FILE_SIZE_BYTES", 10 * 1024 * 1024)
MAX_CONCURRENCY = int_env("VEPAY_API_MAX_CONCURRENCY", min(4, os.cpu_count() or 1))
JOB_TTL_SECONDS = int_env("VEPAY_API_JOB_TTL_SECONDS", 24 * 60 * 60)
REQUIRE_API_KEY = bool_env("VEPAY_API_REQUIRE_API_KEY")
API_KEY = env_value("VEPAY_API_KEY")

OCR_LIMITER = threading.BoundedSemaphore(MAX_CONCURRENCY)


app = FastAPI(
    title=APP_NAME,
    version=APP_VERSION,
    description="Network API for Venezuelan mobile payment receipt OCR.",
)


class ParseErrorItem(BaseModel):
    filename: str
    code: str
    message: str


class ParseSummary(BaseModel):
    total: int
    complete: int
    incomplete: int
    errors: int


class ParseResponse(BaseModel):
    request_id: str
    schema_version: str
    receipts: list[dict[str, Any]]
    summary: ParseSummary
    errors: list[ParseErrorItem]


class ImagePayload(BaseModel):
    filename: str
    image_base64: str
    content_type: str | None = None


class JsonParseRequest(BaseModel):
    images: list[ImagePayload] = Field(..., min_length=1, max_length=MAX_FILES)
    lang: str = DEFAULT_LANG
    include_raw_text: bool = False
    enable_crops: bool = True


class JobCreateRequest(BaseModel):
    images: list[ImagePayload] = Field(..., min_length=1, max_length=MAX_JOB_FILES)
    lang: str = DEFAULT_LANG
    include_raw_text: bool = False
    enable_crops: bool = True


class JobCreateResponse(BaseModel):
    job_id: str
    request_id: str
    status: str
    expires_at: str


class JobStatusResponse(BaseModel):
    job_id: str
    request_id: str | None = None
    status: str
    created_at: str | None = None
    expires_at: str | None = None
    result: ParseResponse | None = None
    error: str | None = None


@dataclass(frozen=True)
class StoredImage:
    filename: str
    path: Path
    content_type: str | None = None


@dataclass
class JobRecord:
    job_id: str
    request_id: str
    status: str
    created_at: datetime
    expires_at: datetime
    result: ParseResponse | None = None
    error: str | None = None


JOBS: dict[str, JobRecord] = {}
JOB_LOCK = asyncio.Lock()


async def require_api_key(
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> None:
    if not REQUIRE_API_KEY:
        return
    if not API_KEY:
        raise HTTPException(
            status_code=500,
            detail="VEPAY_API_REQUIRE_API_KEY is enabled but VEPAY_API_KEY is not set.",
        )
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key.")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def new_request_id() -> str:
    return uuid.uuid4().hex


def safe_filename(filename: str | None, fallback: str = "upload") -> str:
    raw_name = (filename or fallback).replace("\\", "/").split("/")[-1]
    raw_name = raw_name.strip() or fallback
    sanitized = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw_name).strip("._")
    return sanitized or fallback


def normalized_content_type(content_type: str | None) -> str | None:
    if not content_type:
        return None
    return content_type.split(";", 1)[0].strip().lower()


def validate_image_metadata(filename: str, content_type: str | None) -> str | None:
    if Path(filename).suffix.lower() not in IMAGE_EXTENSIONS:
        return "unsupported_extension"
    normalized_type = normalized_content_type(content_type)
    if normalized_type and normalized_type not in ALLOWED_CONTENT_TYPES:
        return "unsupported_content_type"
    return None


def get_tesseract_path() -> str:
    return find_tesseract(env_value("VEPAY_API_TESSERACT"))


def get_temp_parent() -> str | None:
    temp_dir = env_value("VEPAY_API_TEMP_DIR")
    if not temp_dir:
        return None
    path = Path(temp_dir)
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def is_writable_directory(path: str) -> bool:
    probe = Path(path) / ".write-test"
    try:
        probe.write_bytes(b"")
        probe.unlink(missing_ok=True)
    except OSError:
        return False
    return True


@contextmanager
def temporary_directory() -> Iterator[str]:
    temp_parent = get_temp_parent()
    if temp_parent:
        raw_temp_dir = str(Path(temp_parent) / f"vepay_api_{uuid.uuid4().hex}")
        Path(raw_temp_dir).mkdir(parents=True, exist_ok=False)
    else:
        try:
            raw_temp_dir = tempfile.mkdtemp(prefix="vepay_api_")
        except OSError:
            raw_temp_dir = ""
        if not raw_temp_dir or not is_writable_directory(raw_temp_dir):
            if raw_temp_dir:
                shutil.rmtree(raw_temp_dir, ignore_errors=True)
            fallback_parent = Path.cwd() / ".vepay-api-tmp"
            fallback_parent.mkdir(exist_ok=True)
            raw_temp_dir = str(fallback_parent / f"vepay_api_{uuid.uuid4().hex}")
            Path(raw_temp_dir).mkdir(parents=True, exist_ok=False)
    try:
        yield raw_temp_dir
    finally:
        shutil.rmtree(raw_temp_dir, ignore_errors=True)


def list_tesseract_languages(tesseract_path: str) -> list[str]:
    proc = subprocess.run(
        [tesseract_path, "--list-langs"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    output = "\n".join(part for part in (proc.stdout, proc.stderr) if part)
    languages = []
    for line in output.splitlines():
        value = line.strip()
        if not value or value.lower().startswith("list of available languages"):
            continue
        languages.append(value)
    return languages


def api_source_uri(request_id: str, filename: str) -> str:
    return f"upload://{request_id}/{filename}"


def scrub_receipt_for_api(
    receipt: dict[str, Any],
    *,
    request_id: str,
    filename: str,
) -> dict[str, Any]:
    source = receipt.setdefault("source", {})
    source["file_name"] = filename
    source["file_path"] = api_source_uri(request_id, filename)
    return receipt


def build_response(
    *,
    request_id: str,
    total: int,
    receipts: list[dict[str, Any]],
    errors: list[ParseErrorItem],
) -> ParseResponse:
    complete = sum(
        1 for receipt in receipts if receipt.get("validation", {}).get("is_complete")
    )
    incomplete = len(receipts) - complete
    return ParseResponse(
        request_id=request_id,
        schema_version=SCHEMA_VERSION,
        receipts=receipts,
        summary=ParseSummary(
            total=total,
            complete=complete,
            incomplete=incomplete,
            errors=len(errors),
        ),
        errors=errors,
    )


async def save_upload_file(
    upload: UploadFile,
    *,
    index: int,
    temp_dir: Path,
) -> tuple[StoredImage | None, ParseErrorItem | None]:
    filename = safe_filename(upload.filename, fallback=f"upload-{index}")
    metadata_error = validate_image_metadata(filename, upload.content_type)
    if metadata_error:
        return None, ParseErrorItem(
            filename=filename,
            code=metadata_error,
            message="Unsupported image extension or content type.",
        )

    target = temp_dir / f"{index:03d}-{filename}"
    total = 0
    try:
        with target.open("wb") as handle:
            while True:
                chunk = await upload.read(CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_FILE_SIZE_BYTES:
                    try:
                        target.unlink(missing_ok=True)
                    except OSError:
                        pass
                    return None, ParseErrorItem(
                        filename=filename,
                        code="file_too_large",
                        message=f"Image exceeds {MAX_FILE_SIZE_BYTES} bytes.",
                    )
                handle.write(chunk)
    except OSError as exc:
        return None, ParseErrorItem(
            filename=filename,
            code="upload_write_failed",
            message=str(exc),
        )

    if total == 0:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        return None, ParseErrorItem(
            filename=filename,
            code="empty_file",
            message="Uploaded image is empty.",
        )

    return StoredImage(filename=filename, path=target, content_type=upload.content_type), None


def decode_base64_image(payload: ImagePayload) -> bytes:
    image_base64 = payload.image_base64.strip()
    if image_base64.startswith("data:") and "," in image_base64:
        image_base64 = image_base64.split(",", 1)[1]
    image_base64 = re.sub(r"\s+", "", image_base64)
    try:
        return base64.b64decode(image_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Invalid base64 image payload.") from exc


def save_json_image(
    payload: ImagePayload,
    *,
    index: int,
    temp_dir: Path,
) -> tuple[StoredImage | None, ParseErrorItem | None]:
    filename = safe_filename(payload.filename, fallback=f"upload-{index}")
    metadata_error = validate_image_metadata(filename, payload.content_type)
    if metadata_error:
        return None, ParseErrorItem(
            filename=filename,
            code=metadata_error,
            message="Unsupported image extension or content type.",
        )

    try:
        content = decode_base64_image(payload)
    except ValueError as exc:
        return None, ParseErrorItem(
            filename=filename,
            code="invalid_base64",
            message=str(exc),
        )

    if not content:
        return None, ParseErrorItem(
            filename=filename,
            code="empty_file",
            message="Decoded image is empty.",
        )
    if len(content) > MAX_FILE_SIZE_BYTES:
        return None, ParseErrorItem(
            filename=filename,
            code="file_too_large",
            message=f"Image exceeds {MAX_FILE_SIZE_BYTES} bytes.",
        )

    target = temp_dir / f"{index:03d}-{filename}"
    try:
        target.write_bytes(content)
    except OSError as exc:
        return None, ParseErrorItem(
            filename=filename,
            code="upload_write_failed",
            message=str(exc),
        )
    return StoredImage(filename=filename, path=target, content_type=payload.content_type), None


async def parse_stored_images(
    stored_images: list[StoredImage],
    *,
    request_id: str,
    lang: str,
    include_raw_text: bool,
    enable_crops: bool,
) -> tuple[list[dict[str, Any]], list[ParseErrorItem]]:
    try:
        tesseract_path = get_tesseract_path()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    receipts: list[dict[str, Any]] = []
    errors: list[ParseErrorItem] = []
    options = ReceiptOptions(
        tesseract_path=tesseract_path,
        lang=lang,
        include_raw_text=include_raw_text,
        enable_crops=enable_crops,
    )

    for stored in stored_images:
        try:
            receipt = await run_in_threadpool(
                parse_receipt_image_with_limit,
                stored.path,
                options,
            )
            receipts.append(
                scrub_receipt_for_api(
                    receipt,
                    request_id=request_id,
                    filename=stored.filename,
                )
            )
        except Exception as exc:
            errors.append(
                ParseErrorItem(
                    filename=stored.filename,
                    code="ocr_failed",
                    message=str(exc),
                )
            )
    return receipts, errors


def parse_receipt_image_with_limit(
    image_path: Path,
    options: ReceiptOptions,
) -> dict[str, Any]:
    with OCR_LIMITER:
        return parse_receipt_image(image_path, options)


async def parse_json_request(
    body: JsonParseRequest | JobCreateRequest,
    *,
    request_id: str,
) -> ParseResponse:
    with temporary_directory() as raw_temp_dir:
        temp_dir = Path(raw_temp_dir)
        stored_images: list[StoredImage] = []
        errors: list[ParseErrorItem] = []

        for index, payload in enumerate(body.images, start=1):
            stored, error = save_json_image(payload, index=index, temp_dir=temp_dir)
            if stored:
                stored_images.append(stored)
            if error:
                errors.append(error)

        if stored_images:
            receipts, parse_errors = await parse_stored_images(
                stored_images,
                request_id=request_id,
                lang=body.lang,
                include_raw_text=body.include_raw_text,
                enable_crops=body.enable_crops,
            )
        else:
            receipts, parse_errors = [], []
        errors.extend(parse_errors)
        return build_response(
            request_id=request_id,
            total=len(body.images),
            receipts=receipts,
            errors=errors,
        )


async def prune_expired_jobs() -> None:
    now = utc_now()
    async with JOB_LOCK:
        expired = [
            job_id for job_id, record in JOBS.items() if record.expires_at <= now
        ]
        for job_id in expired:
            del JOBS[job_id]


async def update_job(job_id: str, **changes: Any) -> None:
    async with JOB_LOCK:
        record = JOBS.get(job_id)
        if not record:
            return
        for key, value in changes.items():
            setattr(record, key, value)


async def run_job(job_id: str, body: JobCreateRequest) -> None:
    await update_job(job_id, status="running")
    async with JOB_LOCK:
        record = JOBS.get(job_id)
        request_id = record.request_id if record else new_request_id()

    try:
        result = await parse_json_request(body, request_id=request_id)
    except HTTPException as exc:
        await update_job(job_id, status="failed", error=str(exc.detail))
        return
    except Exception as exc:
        await update_job(job_id, status="failed", error=str(exc))
        return

    await update_job(job_id, status="succeeded", result=result)


def is_ui_enabled() -> bool:
    return bool_env("VEPAY_API_ENABLE_UI", False)


def is_ui_default_route_enabled() -> bool:
    return bool_env("VEPAY_API_UI_DEFAULT_ROUTE", False)


def ensure_ui_available() -> None:
    if not is_ui_enabled():
        raise HTTPException(status_code=404, detail="VEPay API UI is disabled.")
    if not CLIENT_DIR.exists():
        raise HTTPException(status_code=404, detail="VEPay API UI assets are unavailable.")


def resolve_client_file(relative_path: str) -> Path:
    ensure_ui_available()
    cleaned = relative_path.replace("\\", "/").lstrip("/")
    target = (CLIENT_DIR / cleaned).resolve()
    client_root = CLIENT_DIR.resolve()
    try:
        target.relative_to(client_root)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="UI asset not found.") from exc
    if not target.is_file():
        raise HTTPException(status_code=404, detail="UI asset not found.")
    return target


def client_file_response(relative_path: str) -> FileResponse:
    return FileResponse(
        resolve_client_file(relative_path),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/ui", include_in_schema=False)
async def audit_client() -> RedirectResponse:
    ensure_ui_available()
    return RedirectResponse(url="/ui/", status_code=307)


@app.get("/ui/", include_in_schema=False)
async def audit_client_slash() -> FileResponse:
    return client_file_response("index.html")


@app.get("/ui/config.js", include_in_schema=False)
async def audit_client_config() -> Response:
    ensure_ui_available()
    config = {
        "apiPrefix": "",
        "mode": "integrated",
        "requireApiKey": REQUIRE_API_KEY,
    }
    return Response(
        f"window.VEPAY_API_CLIENT_CONFIG = {json.dumps(config, sort_keys=True)};\n",
        media_type="application/javascript",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/ui/{asset_path:path}", include_in_schema=False)
async def audit_client_asset(asset_path: str) -> FileResponse:
    return client_file_response(asset_path)


@app.get("/", response_model=None)
async def root() -> dict[str, Any] | RedirectResponse:
    if is_ui_default_route_enabled() and is_ui_enabled():
        ensure_ui_available()
        return RedirectResponse(url="/ui/", status_code=307)
    return {
        "app": APP_NAME,
        "version": APP_VERSION,
        "docs": "/docs",
        "health": "/health",
        "healthz": "/healthz",
        "capabilities": "/v1/capabilities",
        "parse_multipart": "/v1/receipts/parse",
        "parse_json": "/v1/receipts/parse-json",
        "jobs": "/v1/jobs",
    }


@app.get("/health")
@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    try:
        tesseract_path = get_tesseract_path()
        languages = await run_in_threadpool(list_tesseract_languages, tesseract_path)
        return {
            "status": "ok",
            "app": APP_NAME,
            "version": APP_VERSION,
            "tesseract_available": True,
            "languages": languages,
        }
    except Exception as exc:
        message = str(exc)
        if "\\" in message or "/" in message:
            message = "Tesseract is unavailable or misconfigured."
        return {
            "status": "degraded",
            "app": APP_NAME,
            "version": APP_VERSION,
            "tesseract_available": False,
            "error": message,
            "languages": [],
        }


@app.get("/v1/capabilities")
async def capabilities() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "banks": list(SUPPORTED_BANKS),
        "image_extensions": sorted(IMAGE_EXTENSIONS),
        "content_types": sorted(ALLOWED_CONTENT_TYPES),
        "limits": {
            "max_files": MAX_FILES,
            "max_job_files": MAX_JOB_FILES,
            "max_file_size_bytes": MAX_FILE_SIZE_BYTES,
            "max_concurrency": MAX_CONCURRENCY,
            "job_ttl_seconds": JOB_TTL_SECONDS,
        },
        "options": {
            "lang": DEFAULT_LANG,
            "include_raw_text_default": False,
            "enable_crops_default": True,
        },
    }


@app.post("/v1/receipts/parse", response_model=ParseResponse)
async def parse_multipart_receipts(
    files: Annotated[list[UploadFile], File(...)],
    lang: Annotated[str, Form()] = DEFAULT_LANG,
    include_raw_text: Annotated[bool, Form()] = False,
    enable_crops: Annotated[bool, Form()] = True,
    _auth: Annotated[None, Depends(require_api_key)] = None,
) -> ParseResponse:
    if len(files) > MAX_FILES:
        raise HTTPException(
            status_code=413,
            detail=f"At most {MAX_FILES} files are allowed per synchronous request.",
        )

    request_id = new_request_id()
    with temporary_directory() as raw_temp_dir:
        temp_dir = Path(raw_temp_dir)
        stored_images: list[StoredImage] = []
        errors: list[ParseErrorItem] = []

        for index, upload in enumerate(files, start=1):
            stored, error = await save_upload_file(
                upload,
                index=index,
                temp_dir=temp_dir,
            )
            if stored:
                stored_images.append(stored)
            if error:
                errors.append(error)

        if stored_images:
            receipts, parse_errors = await parse_stored_images(
                stored_images,
                request_id=request_id,
                lang=lang,
                include_raw_text=include_raw_text,
                enable_crops=enable_crops,
            )
        else:
            receipts, parse_errors = [], []
        errors.extend(parse_errors)
        return build_response(
            request_id=request_id,
            total=len(files),
            receipts=receipts,
            errors=errors,
        )


@app.post("/v1/receipts/parse-json", response_model=ParseResponse)
async def parse_json_receipts(
    body: JsonParseRequest,
    _auth: Annotated[None, Depends(require_api_key)] = None,
) -> ParseResponse:
    return await parse_json_request(body, request_id=new_request_id())


@app.post("/v1/jobs", status_code=202, response_model=JobCreateResponse)
async def create_job(
    body: JobCreateRequest,
    _auth: Annotated[None, Depends(require_api_key)] = None,
) -> JobCreateResponse:
    await prune_expired_jobs()
    job_id = uuid.uuid4().hex
    request_id = new_request_id()
    created_at = utc_now()
    expires_at = created_at + timedelta(seconds=JOB_TTL_SECONDS)
    record = JobRecord(
        job_id=job_id,
        request_id=request_id,
        status="queued",
        created_at=created_at,
        expires_at=expires_at,
    )
    async with JOB_LOCK:
        JOBS[job_id] = record
    asyncio.create_task(run_job(job_id, body))
    return JobCreateResponse(
        job_id=job_id,
        request_id=request_id,
        status="queued",
        expires_at=iso_timestamp(expires_at),
    )


@app.get("/v1/jobs/{job_id}", response_model=JobStatusResponse)
async def get_job(
    job_id: str,
    _auth: Annotated[None, Depends(require_api_key)] = None,
) -> JobStatusResponse:
    async with JOB_LOCK:
        record = JOBS.get(job_id)

    if not record:
        return JobStatusResponse(job_id=job_id, status="expired")

    if record.expires_at <= utc_now():
        async with JOB_LOCK:
            JOBS.pop(job_id, None)
        return JobStatusResponse(job_id=job_id, status="expired")

    return JobStatusResponse(
        job_id=record.job_id,
        request_id=record.request_id,
        status=record.status,
        created_at=iso_timestamp(record.created_at),
        expires_at=iso_timestamp(record.expires_at),
        result=record.result,
        error=record.error,
    )


def main() -> None:
    import uvicorn

    uvicorn.run(
        "vepay_api:app",
        host=env_value("VEPAY_API_HOST") or "0.0.0.0",
        port=int_env("VEPAY_API_PORT", 8080),
    )


if __name__ == "__main__":
    main()
