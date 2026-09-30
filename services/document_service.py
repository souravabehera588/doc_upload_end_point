"""Document processing: validation, checksum, storage routing."""

from __future__ import annotations

import hashlib
import io
import logging
import mimetypes
import os
import re
import unicodedata
from typing import Dict, List, Optional, Tuple

from fastapi import UploadFile

from config import Settings
from schemas import ErrorCode, FileFailure, FileResultStatus

logger = logging.getLogger(__name__)

# ── Allowed MIME types per extension ─────────────────────────────────────────
# This is the server-side authority — the client-declared Content-Type is
# compared against this; mismatches are rejected.
EXTENSION_MIME_MAP: Dict[str, List[str]] = {
    "pdf":  ["application/pdf"],
    "docx": ["application/vnd.openxmlformats-officedocument.wordprocessingml.document",
             "application/zip"],
    "doc":  ["application/msword", "application/octet-stream"],
    "xlsx": ["application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
             "application/zip"],
    "xls":  ["application/vnd.ms-excel", "application/octet-stream"],
    "pptx": ["application/vnd.openxmlformats-officedocument.presentationml.presentation",
             "application/zip"],
    "ppt":  ["application/vnd.ms-powerpoint", "application/octet-stream"],
    "png":  ["image/png"],
    "jpeg": ["image/jpeg"],
    "jpg":  ["image/jpeg"],
    "gif":  ["image/gif"],
    "tiff": ["image/tiff"],
    "bmp":  ["image/bmp"],
    "txt":  ["text/plain", "application/octet-stream"],
    "csv":  ["text/csv", "text/plain", "application/octet-stream"],
    "zip":  ["application/zip", "application/x-zip-compressed"],
}

# Magic byte signatures for core types (first N bytes)
MAGIC_SIGNATURES: Dict[str, List[bytes]] = {
    "pdf":  [b"%PDF"],
    "docx": [b"PK\x03\x04"],    # ZIP-based Office Open XML
    "xlsx": [b"PK\x03\x04"],
    "pptx": [b"PK\x03\x04"],
    "doc":  [b"\xd0\xcf\x11\xe0"],   # OLE2 compound document
    "xls":  [b"\xd0\xcf\x11\xe0"],
    "ppt":  [b"\xd0\xcf\x11\xe0"],
    "png":  [b"\x89PNG"],
    "jpeg": [b"\xff\xd8\xff"],
    "jpg":  [b"\xff\xd8\xff"],
    "gif":  [b"GIF87a", b"GIF89a"],
    "zip":  [b"PK\x03\x04"],
}


class FileValidationError(Exception):
    def __init__(self, error_code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message    = message


def _sanitise_filename(name: str, max_length: int = 255) -> str:
    """Return a safe filename — strip path separators, control chars, and limit length."""
    name = os.path.basename(name)
    name = unicodedata.normalize("NFKD", name)
    # Keep alphanumerics, dots, dashes, underscores, spaces
    name = re.sub(r"[^\w.\-\s]", "_", name, flags=re.UNICODE)
    name = re.sub(r"\s+", "_", name.strip())
    name = name[:max_length]
    return name or "unnamed_file"


def _extension_from_filename(filename: str) -> str:
    """Return lowercase extension without the dot (e.g. 'pdf')."""
    _, ext = os.path.splitext(filename)
    return ext.lower().lstrip(".")


def _check_magic(data: bytes, extension: str) -> bool:
    """Return True if the first bytes match any known signature for the extension.

    Returns True unconditionally for extensions without a known signature
    (e.g. txt, csv) — we rely on MIME type and extension only for those.
    """
    sigs = MAGIC_SIGNATURES.get(extension)
    if not sigs:
        return True
    return any(data.startswith(sig) for sig in sigs)


async def read_and_validate_file(
    upload: UploadFile,
    settings: Settings,
    seen_filenames: set,
) -> Tuple[bytes, str, str, str]:
    """Read the upload stream and validate it.

    Returns (file_bytes, safe_filename, extension, checksum_hex).
    Raises FileValidationError on any validation failure.
    """
    original_name = upload.filename or "unnamed"
    safe_name     = _sanitise_filename(original_name, settings.max_filename_length)
    extension     = _extension_from_filename(original_name)

    # 1. Allowed extension
    if extension not in settings.allowed_extension_set:
        raise FileValidationError(
            ErrorCode.UNSUPPORTED_FILE_TYPE,
            f"File type '.{extension}' is not allowed. "
            f"Allowed: {', '.join(sorted(settings.allowed_extension_set))}.",
        )

    # 2. Duplicate filename in this batch
    if safe_name in seen_filenames:
        raise FileValidationError(
            ErrorCode.DUPLICATE_FILENAME,
            f"A file named '{safe_name}' was already included in this upload batch.",
        )

    # 3. Filename length
    if len(original_name) > settings.max_filename_length:
        raise FileValidationError(
            ErrorCode.FILENAME_TOO_LONG,
            f"Filename exceeds maximum length of {settings.max_filename_length} characters.",
        )

    # 4. Read content (streaming, up to max size + 1 byte to detect over-limit)
    max_bytes = settings.max_file_size_bytes + 1
    buf = io.BytesIO()
    total_read = 0
    chunk_size = 64 * 1024  # 64 KB chunks

    try:
        while True:
            chunk = await upload.read(chunk_size)
            if not chunk:
                break
            total_read += len(chunk)
            if total_read > settings.max_file_size_bytes:
                raise FileValidationError(
                    ErrorCode.FILE_TOO_LARGE,
                    f"File exceeds maximum allowed size of {settings.max_file_size_mb:.0f} MB.",
                )
            buf.write(chunk)
    except FileValidationError:
        raise
    except Exception as exc:
        raise FileValidationError(
            ErrorCode.FILE_READ_ERROR,
            f"Failed to read file content: {exc}",
        )

    file_bytes = buf.getvalue()

    # 5. Empty file
    if len(file_bytes) == 0:
        raise FileValidationError(
            ErrorCode.EMPTY_FILE,
            "File is empty (0 bytes).",
        )

    # 6. Magic byte check (content matches declared extension)
    if not _check_magic(file_bytes, extension):
        raise FileValidationError(
            ErrorCode.INVALID_CONTENT_TYPE,
            f"File content does not match the declared extension '.{extension}'. "
            "The file may be corrupted or misnamed.",
        )

    # 7. MIME type cross-check (client-declared vs extension-expected)
    client_mime  = (upload.content_type or "").lower().split(";")[0].strip()
    allowed_mimes = EXTENSION_MIME_MAP.get(extension, [])
    if client_mime and allowed_mimes and client_mime not in allowed_mimes:
        # Log a warning but accept — some browsers send generic types for known extensions
        logger.warning(
            "MIME mismatch for '%s': client sent '%s', expected one of %s. Accepting.",
            safe_name, client_mime, allowed_mimes,
        )

    checksum = hashlib.sha256(file_bytes).hexdigest()
    return file_bytes, safe_name, extension, checksum


async def upload_to_s3(
    file_bytes: bytes,
    safe_filename: str,
    content_type: str,
    awf_case_ref: Optional[str],
    settings: Settings,
) -> str:
    """Upload file bytes to S3/ECS and return the object key.

    Raises RuntimeError on failure.
    """
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError as exc:
        raise RuntimeError("boto3 is not installed. Install it to enable S3 storage.") from exc

    kwargs: Dict = {
        "aws_access_key_id":     settings.aws_access_key_id,
        "aws_secret_access_key": settings.aws_secret_access_key,
        "region_name":           settings.aws_region,
    }
    if settings.s3_endpoint_url:
        kwargs["endpoint_url"] = settings.s3_endpoint_url

    s3 = boto3.client("s3", **kwargs)

    case_part = awf_case_ref.replace("|", "_").replace("/", "_") if awf_case_ref else "no_case"
    key = f"{settings.s3_key_prefix}{case_part}/{safe_filename}"

    try:
        s3.put_object(
            Bucket=settings.s3_bucket,
            Key=key,
            Body=file_bytes,
            ContentType=content_type,
            ServerSideEncryption="AES256",
        )
        logger.info("S3 upload OK: s3://%s/%s", settings.s3_bucket, key)
        return key
    except (BotoCoreError, ClientError) as exc:
        raise RuntimeError(f"S3 upload failed: {exc}") from exc


def build_db_payload(
    awf_case_ref:    Optional[str],
    original_name:   str,
    safe_name:       str,
    content_type:    str,
    extension:       str,
    file_bytes:      Optional[bytes],
    checksum:        str,
    storage_type:    str,
    s3_bucket:       Optional[str],
    s3_key:          Optional[str],
    s3_region:       Optional[str],
    document_notes:  Optional[str],
    uploaded_by:     Optional[str],
) -> Dict:
    """Assemble the dict passed to DocumentDB.insert_document."""
    return {
        "awf_case_ref":      awf_case_ref,
        "original_filename": original_name,
        "safe_filename":     safe_name,
        "content_type":      content_type,
        "file_extension":    extension,
        "file_size_bytes":   len(file_bytes) if file_bytes is not None else 0,
        "checksum_sha256":   checksum,
        "storage_type":      storage_type,
        "file_content":      file_bytes,
        "s3_bucket":         s3_bucket,
        "s3_key":            s3_key,
        "s3_region":         s3_region,
        "document_notes":    document_notes,
        "uploaded_by":       uploaded_by,
    }
