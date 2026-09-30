"""Document upload DAO — insert, fetch, soft-delete."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from db.oracle_db import OracleDB, OracleDBError
from queries import document_queries as Q

logger = logging.getLogger(__name__)

__all__ = ["DocumentDB", "DocumentDBError"]


class DocumentDBError(OracleDBError):
    """Raised when the document DB layer cannot serve a request."""


class DocumentDB(OracleDB):
    """DAO for DOCUMENT_UPLOADS and DOCUMENT_ACCESS_LOG tables."""

    error_type = DocumentDBError

    # ── Write ────────────────────────────────────────────────────────────────

    def insert_document(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Insert one document row and return its generated DOCUMENT_ID + CREATED_AT.

        ``payload`` must contain every bind variable referenced in INSERT_DOCUMENT
        (file_content may be None when storage_type='S3').

        Returns dict with keys: ``document_id``, ``created_at``.
        """
        try:
            import oracledb
        except ImportError as exc:
            raise DocumentDBError("oracledb not installed") from exc

        try:
            result = self.execute_returning(
                Q.INSERT_DOCUMENT,
                payload,
                out_vars={
                    "out_document_id": oracledb.DB_TYPE_NUMBER,
                    "out_created_at":  oracledb.DB_TYPE_TIMESTAMP,
                },
            )
        except OracleDBError:
            raise
        except Exception as exc:
            raise self._fail("insert_document", exc) from exc

        doc_id    = result.get("out_document_id")
        created   = result.get("out_created_at")

        if doc_id is None:
            raise DocumentDBError("INSERT returned no DOCUMENT_ID — row may not have been written")

        # oracledb returns list from RETURNING when row-count > 1; unwrap if so
        if isinstance(doc_id, list):
            doc_id  = doc_id[0]
        if isinstance(created, list):
            created = created[0]

        return {
            "document_id": int(doc_id),
            "created_at":  str(created) if created else None,
        }

    def log_access(
        self,
        document_id: int,
        access_type: str,
        accessed_by: Optional[str] = None,
        ip_address: Optional[str] = None,
    ) -> None:
        """Append a row to DOCUMENT_ACCESS_LOG (best-effort; errors are swallowed)."""
        try:
            self.execute(
                Q.INSERT_ACCESS_LOG,
                {
                    "document_id": document_id,
                    "access_type": access_type,
                    "accessed_by": accessed_by,
                    "ip_address":  ip_address,
                },
            )
        except Exception as exc:
            logger.warning("log_access failed (non-fatal): %s", exc)

    def soft_delete(self, document_id: int) -> bool:
        """Mark a document as DELETED. Returns True if a row was updated."""
        try:
            rows = self.execute(Q.SOFT_DELETE_DOCUMENT, {"document_id": document_id})
            return rows > 0
        except OracleDBError:
            raise
        except Exception as exc:
            raise self._fail("soft_delete", exc) from exc

    # ── Read ─────────────────────────────────────────────────────────────────

    def get_document(self, document_id: int) -> Optional[Dict[str, Any]]:
        """Fetch document metadata + content by ID. Returns None if not found."""
        return self.fetch_one(Q.FETCH_DOCUMENT_BY_ID, {"document_id": document_id})

    def list_by_case(self, awf_case_ref: Optional[str]) -> List[Dict[str, Any]]:
        """List all non-deleted documents, optionally filtered by case (newest first)."""
        if awf_case_ref:
            return self.fetch_all(Q.FETCH_DOCUMENTS_BY_CASE, {"awf_case_ref": awf_case_ref})
        return self.fetch_all(Q.FETCH_ALL_DOCUMENTS)
