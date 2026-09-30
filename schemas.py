"""Pydantic request / response models for the document upload service."""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


# ── Enumerations ──────────────────────────────────────────────────────────────

class StorageType(str, Enum):
    ORACLE_BLOB = "ORACLE_BLOB"
    S3          = "S3"
    BOTH        = "BOTH"
    LOCAL       = "LOCAL"
    MINIO       = "MINIO"


class UploadStatus(str, Enum):
    UPLOADED = "UPLOADED"
    FAILED   = "FAILED"
    DELETED  = "DELETED"


class FileResultStatus(str, Enum):
    SUCCESS  = "success"
    REJECTED = "rejected"
    ERROR    = "error"


# ── Error codes (structured so the frontend can branch on them) ───────────────

class ErrorCode(str, Enum):
    NO_FILES              = "NO_FILES"
    TOO_MANY_FILES        = "TOO_MANY_FILES"
    EMPTY_FILE            = "EMPTY_FILE"
    FILE_TOO_LARGE        = "FILE_TOO_LARGE"
    TOTAL_SIZE_EXCEEDED   = "TOTAL_SIZE_EXCEEDED"
    UNSUPPORTED_FILE_TYPE = "UNSUPPORTED_FILE_TYPE"
    INVALID_CONTENT_TYPE  = "INVALID_CONTENT_TYPE"
    FILENAME_TOO_LONG     = "FILENAME_TOO_LONG"
    DUPLICATE_FILENAME    = "DUPLICATE_FILENAME"
    DB_ERROR              = "DB_ERROR"
    S3_ERROR              = "S3_ERROR"
    FILE_READ_ERROR       = "FILE_READ_ERROR"
    INTERNAL_ERROR        = "INTERNAL_ERROR"
    DOCUMENT_NOT_FOUND    = "DOCUMENT_NOT_FOUND"
    ALREADY_DELETED       = "ALREADY_DELETED"


# ── Per-file upload result ────────────────────────────────────────────────────

class FileSuccess(BaseModel):
    filename:        str
    document_id:     int
    content_type:    str
    file_extension:  str
    file_size_bytes: int
    checksum_sha256: str
    storage_type:    StorageType
    s3_key:          Optional[str] = None
    created_at:      Optional[str] = None
    status:          FileResultStatus = FileResultStatus.SUCCESS


class FileFailure(BaseModel):
    filename:      str
    status:        FileResultStatus
    error_code:    ErrorCode
    error_message: str


class UploadSummary(BaseModel):
    total_received:     int
    successful:         int
    failed:             int
    total_size_bytes:   int   = Field(description="Sum of all successfully uploaded file sizes")


# ── Upload response ───────────────────────────────────────────────────────────

class DocumentUploadResponse(BaseModel):
    """Returned by POST /documents/upload — always HTTP 200/207."""
    uploaded: List[FileSuccess] = Field(default_factory=list)
    failed:   List[FileFailure] = Field(default_factory=list)
    summary:  UploadSummary


# ── Document info (no content) ────────────────────────────────────────────────

class DocumentInfo(BaseModel):
    document_id:     int
    awf_case_ref:    Optional[str] = None
    original_filename: str
    content_type:    str
    file_extension:  str
    file_size_bytes: int
    checksum_sha256: Optional[str] = None
    storage_type:    StorageType
    s3_bucket:       Optional[str] = None
    s3_key:          Optional[str] = None
    upload_status:   UploadStatus
    uploaded_by:     Optional[str] = None
    created_at:      Optional[str] = None


class DocumentListResponse(BaseModel):
    documents: List[DocumentInfo]
    count:     int


# ── Download response is StreamingResponse (no Pydantic model) ───────────────

# ── Delete response ───────────────────────────────────────────────────────────

class DeleteResponse(BaseModel):
    document_id: int
    status:      str = "deleted"
    message:     str


# ── Simple upload status response ─────────────────────────────────────────────

class FileUploadResult(BaseModel):
    filename:    str
    status:      bool
    message:     str
    document_id: Optional[int] = None
    bucket:      Optional[str] = None
    object_key:  Optional[str] = None


class UploadStatusResponse(BaseModel):
    status:   bool
    message:  str
    uploaded: List[str]            = Field(default_factory=list)
    failed:   List[FileUploadResult] = Field(default_factory=list)


# ── Standard error response ───────────────────────────────────────────────────

class ErrorResponse(BaseModel):
    error_code:    ErrorCode
    error_message: str
    detail:        Optional[Any] = None
