"""FastAPI router — document upload, list, download, delete."""

from __future__ import annotations

import logging
from typing import Annotated, List, Optional
from urllib.parse import quote

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response, StreamingResponse

import schemas
from config import get_settings
from db.document_db import DocumentDB, DocumentDBError
from db.local_db import LocalDocumentDB, LocalDocumentDBError
from db.minio_db import MinioDocumentDB, MinioDocumentDBError
from schemas import (
    DeleteResponse,
    DocumentInfo,
    DocumentListResponse,
    DocumentUploadResponse,
    ErrorCode,
    ErrorResponse,
    FileFailure,
    FileResultStatus,
    FileSuccess,
    FileUploadResult,
    StorageType,
    UploadStatusResponse,
    UploadSummary,
)
from services.document_service import (
    FileValidationError,
    build_db_payload,
    read_and_validate_file,
    upload_to_s3,
)

_DB_ERRORS = (DocumentDBError, LocalDocumentDBError, MinioDocumentDBError)


def _get_db(settings):
    """Return the right DB instance for the configured storage strategy."""
    if settings.storage_strategy == "local":
        return LocalDocumentDB()
    if settings.storage_strategy == "minio":
        return MinioDocumentDB()
    return DocumentDB()

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/documents", tags=["documents"])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _client_ip(request: Request) -> Optional[str]:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


def _uploader_id(request: Request) -> Optional[str]:
    """Extract uploader identity from Bearer token or X-LAN-ID header."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip() or None
    return request.headers.get("X-LAN-ID") or None


# ── POST /documents/upload ────────────────────────────────────────────────────

@router.post(
    "/upload",
    response_model=UploadStatusResponse,
    status_code=200,
    summary="Upload a document",
    description=(
        "Accepts a single file (PDF, DOCX, XLSX, PNG, JPEG, etc.) with a case reference and step. "
        "The file is validated (type, size, content) and stored in the configured backend."
    ),
    responses={
        200: {"description": "Upload result with status flag and message"},
        400: {"model": ErrorResponse, "description": "Request-level validation failure"},
        503: {"model": ErrorResponse, "description": "Storage backend unavailable"},
    },
)
async def upload_documents(
    request: Request,
    files:        List[UploadFile] = File(..., description="One or more files to upload"),
    awf_case_ref: Annotated[str, Query(description="Case reference to attach the documents to")] = ...,
    step:         Annotated[str, Query(description="Process step associated with this upload")] = ...,
) -> UploadStatusResponse:

    settings  = get_settings()
    uploader  = _uploader_id(request)
    client_ip = _client_ip(request)

    # ── Storage connectivity probe ────────────────────────────────────────────
    try:
        db = _get_db(settings)
        if settings.storage_strategy == "minio":
            db.ping()
        elif settings.storage_strategy not in ("local",):
            db.fetch_one("SELECT 1 AS ping FROM DUAL")
    except _DB_ERRORS as db_exc:
        logger.error("DB unavailable — aborting upload: %s", db_exc)
        raise HTTPException(
            status_code=503,
            detail=ErrorResponse(
                error_code=ErrorCode.DB_ERROR,
                error_message="Storage backend is currently unavailable. Please try again later.",
            ).model_dump(),
        )

    uploaded: List[FileUploadResult] = []
    failed:   List[FileUploadResult] = []
    seen_filenames: set = set()

    for file in files:
        original_name = file.filename or "unnamed"

        # ── Validate and read ─────────────────────────────────────────────────
        try:
            file_bytes, safe_name, extension, checksum = await read_and_validate_file(
                file, settings, seen_filenames
            )
        except FileValidationError as ve:
            logger.info("validation reject: file='%s' code=%s", original_name, ve.error_code)
            failed.append(FileUploadResult(filename=original_name, status=False, message=ve.message))
            continue

        if len(file_bytes) > settings.max_total_size_bytes:
            failed.append(FileUploadResult(
                filename=original_name, status=False,
                message=f"File exceeds size limit of {settings.max_total_size_mb:.0f} MB.",
            ))
            continue

        seen_filenames.add(safe_name)
        content_type = (file.content_type or "application/octet-stream").split(";")[0]

        # ── Determine storage strategy ────────────────────────────────────────
        storage_type = settings.storage_strategy
        if storage_type == "auto":
            storage_type = "oracle" if len(file_bytes) <= settings.auto_blob_threshold_bytes else "s3"
        if storage_type in ("s3", "both") and not settings.s3_enabled:
            logger.warning("S3 requested but not configured; falling back to Oracle BLOB")
            storage_type = "oracle"

        # ── S3 upload (when applicable) ───────────────────────────────────────
        s3_key = s3_bucket = s3_region = None
        if storage_type in ("s3", "both"):
            try:
                s3_key    = await upload_to_s3(file_bytes, safe_name, content_type, awf_case_ref, settings)
                s3_bucket = settings.s3_bucket
                s3_region = settings.aws_region
                if storage_type == "s3":
                    file_bytes = None
            except RuntimeError as s3_exc:
                logger.error("S3 upload failed for '%s': %s", safe_name, s3_exc)
                if storage_type == "s3":
                    failed.append(FileUploadResult(
                        filename=original_name, status=False,
                        message=f"Object storage upload failed: {s3_exc}",
                    ))
                    continue
                storage_type = "oracle"

        # ── DB insert ─────────────────────────────────────────────────────────
        payload = build_db_payload(
            awf_case_ref=awf_case_ref,
            original_name=original_name,
            safe_name=safe_name,
            content_type=content_type,
            extension=extension,
            file_bytes=file_bytes,
            checksum=checksum,
            storage_type=storage_type.upper(),
            s3_bucket=s3_bucket,
            s3_key=s3_key,
            s3_region=s3_region,
            document_notes=f"step={step}",
            uploaded_by=uploader,
        )

        try:
            result = db.insert_document(payload)
        except _DB_ERRORS as db_exc:
            logger.error("DB insert failed for '%s': %s", safe_name, db_exc)
            failed.append(FileUploadResult(
                filename=original_name, status=False,
                message="Storage backend error — file was not saved.",
            ))
            continue

        db.log_access(result["document_id"], "UPLOAD", accessed_by=uploader, ip_address=client_ip)
        logger.info(
            "upload OK: doc_id=%s file='%s' case=%s step=%s uploader=%s",
            result["document_id"], safe_name, awf_case_ref, step, uploader,
        )
        uploaded.append(original_name)

    total    = len(uploaded) + len(failed)
    all_ok   = len(failed) == 0
    return UploadStatusResponse(
        status=all_ok,
        message=(
            f"{len(uploaded)} of {total} file(s) uploaded successfully."
            if not all_ok else
            f"All {total} file(s) uploaded successfully."
        ),
        uploaded=uploaded,
        failed=failed,
    )


# ── GET /documents/{document_id} ─────────────────────────────────────────────

@router.get(
    "/{document_id}/info",
    response_model=DocumentInfo,
    summary="Get document metadata (no content)",
    responses={
        404: {"model": ErrorResponse, "description": "Document not found"},
        503: {"model": ErrorResponse, "description": "Database unavailable"},
    },
)
async def get_document_info(document_id: int, request: Request) -> DocumentInfo:
    try:
        db  = _get_db(get_settings())
        row = db.get_document(document_id)
    except _DB_ERRORS as exc:
        logger.warning("get_document_info DB error doc_id=%s: %s", document_id, exc)
        raise HTTPException(status_code=503, detail={"error_code": ErrorCode.DB_ERROR,
                                                      "error_message": "Database unavailable"})

    if not row:
        raise HTTPException(
            status_code=404,
            detail={"error_code": ErrorCode.DOCUMENT_NOT_FOUND,
                    "error_message": f"Document {document_id!r} not found."},
        )

    return DocumentInfo(
        document_id=row["document_id"],
        awf_case_ref=row.get("awf_case_ref"),
        original_filename=row["original_filename"],
        content_type=row["content_type"],
        file_extension=row["file_extension"],
        file_size_bytes=row["file_size_bytes"],
        checksum_sha256=row.get("checksum_sha256"),
        storage_type=StorageType(row["storage_type"]),
        s3_bucket=row.get("s3_bucket"),
        s3_key=row.get("s3_key"),
        upload_status=schemas.UploadStatus(row["upload_status"]),
        uploaded_by=row.get("uploaded_by"),
        created_at=row.get("created_at"),
    )


# ── GET /documents/{document_id}/download ────────────────────────────────────

@router.get(
    "/{document_id}/download",
    summary="Download a document's binary content",
    responses={
        200: {"content": {"application/octet-stream": {}}},
        404: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
)
async def download_document(document_id: int, request: Request):
    uploader  = _uploader_id(request)
    client_ip = _client_ip(request)

    try:
        db  = _get_db(get_settings())
        row = db.get_document(document_id)
    except _DB_ERRORS as exc:
        logger.warning("download DB error doc_id=%s: %s", document_id, exc)
        raise HTTPException(status_code=503, detail={"error_code": "DB_ERROR",
                                                      "error_message": "Database unavailable"})

    if not row:
        raise HTTPException(
            status_code=404,
            detail={"error_code": "DOCUMENT_NOT_FOUND",
                    "error_message": f"Document {document_id!r} not found."},
        )

    content      = row.get("file_content")
    storage_type = (row.get("storage_type") or "ORACLE_BLOB").upper()

    # S3 retrieval
    if storage_type in ("S3",) or (storage_type == "BOTH" and not content):
        settings = get_settings()
        try:
            import boto3
            kwargs: dict = {
                "aws_access_key_id":     settings.aws_access_key_id,
                "aws_secret_access_key": settings.aws_secret_access_key,
                "region_name":           settings.aws_region,
            }
            if settings.s3_endpoint_url:
                kwargs["endpoint_url"] = settings.s3_endpoint_url
            s3  = boto3.client("s3", **kwargs)
            obj = s3.get_object(Bucket=row["s3_bucket"], Key=row["s3_key"])
            content = obj["Body"].read()
        except Exception as exc:
            logger.error("S3 download failed for doc_id=%s: %s", document_id, exc)
            raise HTTPException(status_code=503, detail={"error_code": "S3_ERROR",
                                                          "error_message": "Object storage unavailable"})

    if not content:
        raise HTTPException(
            status_code=404,
            detail={"error_code": "DOCUMENT_NOT_FOUND",
                    "error_message": "Document content is not available."},
        )

    # Log download (best-effort)
    db.log_access(document_id, "DOWNLOAD", accessed_by=uploader, ip_address=client_ip)

    filename     = row.get("original_filename", f"document_{document_id}")
    content_type = row.get("content_type", "application/octet-stream")

    # RFC 5987 encoding ensures the original filename (including spaces, unicode,
    # special chars) is preserved when the browser saves to the OS Downloads folder.
    encoded_filename = quote(filename, safe="")
    disposition = (
        f'attachment; filename="{filename}"; '
        f"filename*=UTF-8''{encoded_filename}"
    )

    return Response(
        content=content,
        media_type=content_type,
        headers={
            "Content-Disposition":       disposition,
            "Content-Length":            str(len(content)),
            "X-Document-Id":             str(document_id),
            "Access-Control-Expose-Headers": "Content-Disposition, X-Document-Id",
        },
    )


# ── GET /documents/ (list by case) ───────────────────────────────────────────

@router.get(
    "/",
    response_model=DocumentListResponse,
    summary="List documents",
    responses={
        503: {"model": ErrorResponse, "description": "Database unavailable"},
    },
)
async def list_documents(
    awf_case_ref: Annotated[Optional[str], Query(description="Filter by case reference (omit to return all)")] = None,
) -> DocumentListResponse:

    try:
        db   = _get_db(get_settings())
        rows = db.list_by_case(awf_case_ref)
    except _DB_ERRORS as exc:
        logger.warning("list_documents DB error case=%s: %s", awf_case_ref, exc)
        raise HTTPException(status_code=503, detail={"error_code": "DB_ERROR",
                                                      "error_message": "Database unavailable"})

    docs = [
        DocumentInfo(
            document_id=r["document_id"],
            awf_case_ref=r.get("awf_case_ref"),
            original_filename=r["original_filename"],
            content_type=r["content_type"],
            file_extension=r["file_extension"],
            file_size_bytes=r["file_size_bytes"],
            checksum_sha256=r.get("checksum_sha256"),
            storage_type=StorageType(r["storage_type"]),
            s3_bucket=r.get("s3_bucket"),
            s3_key=r.get("s3_key"),
            upload_status=schemas.UploadStatus(r["upload_status"]),
            uploaded_by=r.get("uploaded_by"),
            created_at=r.get("created_at"),
        )
        for r in rows
    ]
    return DocumentListResponse(documents=docs, count=len(docs))


# ── DELETE /documents/{document_id} ──────────────────────────────────────────

@router.delete(
    "/{document_id}",
    response_model=DeleteResponse,
    summary="Soft-delete a document",
    responses={
        404: {"model": ErrorResponse, "description": "Document not found"},
        503: {"model": ErrorResponse, "description": "Database unavailable"},
    },
)
async def delete_document(document_id: int, request: Request) -> DeleteResponse:
    uploader  = _uploader_id(request)
    client_ip = _client_ip(request)

    try:
        db      = _get_db(get_settings())
        deleted = db.soft_delete(document_id)
    except _DB_ERRORS as exc:
        logger.warning("delete DB error doc_id=%s: %s", document_id, exc)
        raise HTTPException(status_code=503, detail={"error_code": "DB_ERROR",
                                                      "error_message": "Database unavailable"})

    if not deleted:
        raise HTTPException(
            status_code=404,
            detail={"error_code": ErrorCode.DOCUMENT_NOT_FOUND,
                    "error_message": f"Document {document_id!r} not found or already deleted."},
        )

    db.log_access(document_id, "DELETE", accessed_by=uploader, ip_address=client_ip)
    logger.info("soft-deleted doc_id=%s by %s from %s", document_id, uploader, client_ip)

    return DeleteResponse(
        document_id=document_id,
        status="deleted",
        message=f"Document {document_id} has been marked as deleted.",
    )
