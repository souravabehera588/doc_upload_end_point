"""Local filesystem + SQLite storage — drop-in replacement for DocumentDB."""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

__all__ = ["LocalDocumentDB", "LocalDocumentDBError"]


class LocalDocumentDBError(Exception):
    """Raised when the local storage layer cannot serve a request."""


_init_lock = threading.Lock()
_initialized: set = set()

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
    storage_type      TEXT    DEFAULT 'LOCAL',
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


class LocalDocumentDB:
    """SQLite metadata + local filesystem binary storage.

    Environment variables (read from .env via the app's dotenv load):
        LOCAL_STORAGE_PATH  directory for file binaries  (default: uploads/files)
        LOCAL_DB_PATH       path to the SQLite database  (default: uploads/documents.db)
    """

    def __init__(self) -> None:
        self._db_path   = os.environ.get("LOCAL_DB_PATH",      "uploads/documents.db")
        self._files_dir = os.environ.get("LOCAL_STORAGE_PATH", "uploads/files")

        Path(self._files_dir).mkdir(parents=True, exist_ok=True)
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)

        with _init_lock:
            if self._db_path not in _initialized:
                self._init_schema()
                _initialized.add(self._db_path)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    # ── Write ─────────────────────────────────────────────────────────────────

    def insert_document(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        file_content = payload.get("file_content")
        row_data = {k: v for k, v in payload.items() if k != "file_content"}

        try:
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
                         :checksum_sha256, :storage_type, NULL,
                         :s3_bucket, :s3_key, :s3_region,
                         :document_notes, 'UPLOADED', :uploaded_by)
                    """,
                    row_data,
                )
                doc_id = cur.lastrowid

            if file_content:
                file_path = str(
                    Path(self._files_dir) / f"{doc_id}_{payload['safe_filename']}"
                )
                with open(file_path, "wb") as fh:
                    fh.write(file_content)
                with self._conn() as conn:
                    conn.execute(
                        "UPDATE document_uploads SET file_path = ? WHERE document_id = ?",
                        (file_path, doc_id),
                    )

            return {
                "document_id": doc_id,
                "created_at":  datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        except LocalDocumentDBError:
            raise
        except Exception as exc:
            raise LocalDocumentDBError(f"insert_document failed: {exc}") from exc

    def soft_delete(self, document_id: int) -> bool:
        try:
            with self._conn() as conn:
                cur = conn.execute(
                    "UPDATE document_uploads "
                    "SET upload_status = 'DELETED', "
                    "    updated_at    = strftime('%Y-%m-%dT%H:%M:%SZ','now') "
                    "WHERE document_id = ? AND upload_status != 'DELETED'",
                    (document_id,),
                )
            return cur.rowcount > 0
        except Exception as exc:
            raise LocalDocumentDBError(f"soft_delete failed: {exc}") from exc

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
            file_path = result.get("file_path")
            if file_path and os.path.exists(file_path):
                with open(file_path, "rb") as fh:
                    result["file_content"] = fh.read()
            else:
                result["file_content"] = None

            return result
        except Exception as exc:
            raise LocalDocumentDBError(f"get_document failed: {exc}") from exc

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
            raise LocalDocumentDBError(f"list_by_case failed: {exc}") from exc
