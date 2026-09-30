"""MinIO object storage + SQLite metadata — drop-in replacement for DocumentDB."""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

__all__ = ["MinioDocumentDB", "MinioDocumentDBError"]


class MinioDocumentDBError(Exception):
    """Raised when the MinIO/SQLite storage layer cannot serve a request."""


_init_lock = threading.Lock()
_initialized: set = set()

# Identical schema to local_db.py so the same SQLite tooling works across both.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS document_uploads (
    document_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    awf_case_ref      TEXT,
    original_filename TEXT    NOT NULL,
    safe_filename     TEXT    NOT NULL,
    content_type      TEXT,
    file_extension    TEXT,
    file_size_bytes   INTEGER,
    checksum_sha256   TEXT,
    storage_type      TEXT    DEFAULT 'MINIO',
    file_path         TEXT,
    s3_bucket         TEXT,
    s3_key            TEXT,
    s3_region         TEXT,
    document_notes    TEXT,
    upload_status     TEXT    DEFAULT 'UPLOADED',
    uploaded_by       TEXT,
    created_at        TEXT    DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at        TEXT    DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE TABLE IF NOT EXISTS document_access_log (
    log_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id   INTEGER,
    access_type   TEXT,
    accessed_by   TEXT,
    ip_address    TEXT,
    accessed_at   TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
"""

_LIST_COLS = (
    "document_id", "awf_case_ref", "original_filename", "content_type",
    "file_extension", "file_size_bytes", "checksum_sha256", "storage_type",
    "s3_bucket", "s3_key", "upload_status", "uploaded_by", "created_at",
)


class MinioDocumentDB:
    """SQLite metadata + MinIO binary storage.

    Environment variables (read from .env via the app's dotenv load):
        MINIO_ENDPOINT_URL  MinIO server URL            (e.g. http://localhost:9000)
        MINIO_ACCESS_KEY    MinIO access key            (default: minioadmin)
        MINIO_SECRET_KEY    MinIO secret key            (default: minioadmin)
        MINIO_BUCKET        Bucket name                 (default: documents)
        MINIO_KEY_PREFIX    Object key prefix           (default: documents/)
        MINIO_SECURE        Use HTTPS (true/false)      (default: false)
        MINIO_DB_PATH       SQLite metadata path        (default: uploads/minio_documents.db)

    Files are stored as MinIO objects; their bucket/key is recorded in the
    s3_bucket / s3_key columns so the download and delete paths work without
    any schema changes.
    """

    def __init__(self) -> None:
        self._endpoint   = os.environ.get("MINIO_ENDPOINT_URL", "http://localhost:9000")
        self._access_key = os.environ.get("MINIO_ACCESS_KEY", "minioadmin")
        self._secret_key = os.environ.get("MINIO_SECRET_KEY", "minioadmin")
        self._bucket     = os.environ.get("MINIO_BUCKET", "documents")
        self._key_prefix = os.environ.get("MINIO_KEY_PREFIX", "documents/")
        self._db_path    = os.environ.get("MINIO_DB_PATH", "uploads/minio_documents.db")

        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)

        with _init_lock:
            if self._db_path not in _initialized:
                self._init_schema()
                _initialized.add(self._db_path)

        self._client = self._build_client()
        self._ensure_bucket()

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _build_client(self):
        import boto3
        return boto3.client(
            "s3",
            endpoint_url=self._endpoint,
            aws_access_key_id=self._access_key,
            aws_secret_access_key=self._secret_key,
        )

    def _ensure_bucket(self) -> None:
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except Exception:
            try:
                self._client.create_bucket(Bucket=self._bucket)
                logger.info("MinIO bucket created: %s", self._bucket)
            except Exception as exc:
                logger.warning("Could not create MinIO bucket '%s': %s", self._bucket, exc)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    def _minio_key(self, awf_case_ref: Optional[str], doc_id: int, safe_filename: str) -> str:
        case_part = awf_case_ref.replace("|", "_").replace("/", "_") if awf_case_ref else "no_case"
        return f"{self._key_prefix}{case_part}/{safe_filename}"

    # ── Connectivity probe (used by routes and health check) ──────────────────

    def ping(self) -> bool:
        """Return True if MinIO is reachable; raise MinioDocumentDBError otherwise."""
        try:
            self._client.list_buckets()
            return True
        except Exception as exc:
            raise MinioDocumentDBError(f"MinIO unreachable: {exc}") from exc

    # ── Write ─────────────────────────────────────────────────────────────────

    def insert_document(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        file_content = payload.get("file_content")
        row_data = {k: v for k, v in payload.items() if k != "file_content"}

        try:
            # 1. Insert metadata row to get the auto-generated document_id.
            with self._conn() as conn:
                cur = conn.execute(
                    """
                    INSERT INTO document_uploads
                        (awf_case_ref, original_filename, safe_filename,
                         content_type, file_extension, file_size_bytes,
                         checksum_sha256, storage_type, file_path,
                         s3_bucket, s3_key, s3_region,
                         document_notes, upload_status, uploaded_by)
                    VALUES
                        (:awf_case_ref, :original_filename, :safe_filename,
                         :content_type, :file_extension, :file_size_bytes,
                         :checksum_sha256, 'MINIO', NULL,
                         NULL, NULL, NULL,
                         :document_notes, 'UPLOADED', :uploaded_by)
                    """,
                    row_data,
                )
                doc_id = cur.lastrowid

            # 2. Upload binary to MinIO.
            minio_key = None
            if file_content:
                minio_key = self._minio_key(
                    payload.get("awf_case_ref"), doc_id, payload["safe_filename"]
                )
                self._client.put_object(
                    Bucket=self._bucket,
                    Key=minio_key,
                    Body=file_content,
                    ContentType=payload.get("content_type", "application/octet-stream"),
                )
                # 3. Record the object location in SQLite.
                with self._conn() as conn:
                    conn.execute(
                        "UPDATE document_uploads SET s3_bucket = ?, s3_key = ? WHERE document_id = ?",
                        (self._bucket, minio_key, doc_id),
                    )
                logger.info("MinIO upload OK: %s/%s", self._bucket, minio_key)

            return {
                "document_id":  doc_id,
                "created_at":   datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "minio_bucket": self._bucket if minio_key else None,
                "minio_key":    minio_key,
            }
        except MinioDocumentDBError:
            raise
        except Exception as exc:
            raise MinioDocumentDBError(f"insert_document failed: {exc}") from exc

    def soft_delete(self, document_id: int) -> bool:
        try:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT s3_bucket, s3_key FROM document_uploads "
                    "WHERE document_id = ? AND upload_status != 'DELETED'",
                    (document_id,),
                ).fetchone()

                if row is None:
                    return False

                conn.execute(
                    "UPDATE document_uploads "
                    "SET upload_status = 'DELETED', "
                    "    updated_at    = strftime('%Y-%m-%dT%H:%M:%SZ','now') "
                    "WHERE document_id = ? AND upload_status != 'DELETED'",
                    (document_id,),
                )

            # Best-effort object deletion from MinIO.
            if row["s3_key"]:
                try:
                    self._client.delete_object(Bucket=row["s3_bucket"] or self._bucket, Key=row["s3_key"])
                    logger.info("MinIO object deleted: %s", row["s3_key"])
                except Exception as exc:
                    logger.warning("MinIO delete failed for key '%s': %s", row["s3_key"], exc)

            return True
        except MinioDocumentDBError:
            raise
        except Exception as exc:
            raise MinioDocumentDBError(f"soft_delete failed: {exc}") from exc

    def log_access(
        self,
        document_id: int,
        access_type: str,
        accessed_by: Optional[str] = None,
        ip_address:  Optional[str] = None,
    ) -> None:
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO document_access_log "
                    "(document_id, access_type, accessed_by, ip_address) "
                    "VALUES (?, ?, ?, ?)",
                    (document_id, access_type, accessed_by, ip_address),
                )
        except Exception as exc:
            logger.warning("log_access failed (non-fatal): %s", exc)

    # ── Read ──────────────────────────────────────────────────────────────────

    def get_document(self, document_id: int) -> Optional[Dict[str, Any]]:
        try:
            with self._conn() as conn:
                cur = conn.execute(
                    "SELECT * FROM document_uploads "
                    "WHERE document_id = ? AND upload_status != 'DELETED'",
                    (document_id,),
                )
                row = cur.fetchone()

            if row is None:
                return None

            result = dict(row)

            # Fetch binary content from MinIO.
            minio_key = result.get("s3_key")
            bucket    = result.get("s3_bucket") or self._bucket
            if minio_key:
                try:
                    obj = self._client.get_object(Bucket=bucket, Key=minio_key)
                    result["file_content"] = obj["Body"].read()
                except Exception as exc:
                    logger.error("MinIO get_object failed for key '%s': %s", minio_key, exc)
                    result["file_content"] = None
            else:
                result["file_content"] = None

            return result
        except Exception as exc:
            raise MinioDocumentDBError(f"get_document failed: {exc}") from exc

    def list_by_case(self, awf_case_ref: Optional[str]) -> List[Dict[str, Any]]:
        cols = ", ".join(f'"{c}"' for c in _LIST_COLS)
        try:
            with self._conn() as conn:
                if awf_case_ref:
                    cur = conn.execute(
                        f"SELECT {cols} FROM document_uploads "
                        "WHERE awf_case_ref = ? AND upload_status != 'DELETED' "
                        "ORDER BY created_at DESC",
                        (awf_case_ref,),
                    )
                else:
                    cur = conn.execute(
                        f"SELECT {cols} FROM document_uploads "
                        "WHERE upload_status != 'DELETED' "
                        "ORDER BY created_at DESC"
                    )
                return [dict(r) for r in cur.fetchall()]
        except Exception as exc:
            raise MinioDocumentDBError(f"list_by_case failed: {exc}") from exc
