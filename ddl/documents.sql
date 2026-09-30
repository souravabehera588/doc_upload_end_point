-- =============================================================================
-- Document Uploads DDL
-- =============================================================================
-- Run as the owning schema user (e.g. GCTST_ADMIN).
-- Compatible with Oracle 19c+.
-- =============================================================================

-- Main upload registry table
CREATE TABLE "DOCUMENT_UPLOADS" (
    "DOCUMENT_ID"        NUMBER GENERATED ALWAYS AS IDENTITY (START WITH 1 INCREMENT BY 1) PRIMARY KEY,
    "AWF_CASE_REF"       VARCHAR2(200 CHAR),
    "ORIGINAL_FILENAME"  VARCHAR2(500 CHAR)  NOT NULL,
    "SAFE_FILENAME"      VARCHAR2(500 CHAR)  NOT NULL,
    "CONTENT_TYPE"       VARCHAR2(200 CHAR)  NOT NULL,
    "FILE_EXTENSION"     VARCHAR2(20  CHAR)  NOT NULL,
    "FILE_SIZE_BYTES"    NUMBER(15)          NOT NULL,
    "CHECKSUM_SHA256"    VARCHAR2(64  CHAR),
    -- Storage: ORACLE_BLOB stores inline; S3 stores object reference
    "STORAGE_TYPE"       VARCHAR2(20  CHAR)  NOT NULL DEFAULT 'ORACLE_BLOB'
                            CONSTRAINT chk_doc_storage CHECK ("STORAGE_TYPE" IN ('ORACLE_BLOB','S3','BOTH')),
    "FILE_CONTENT"       BLOB,
    "S3_BUCKET"          VARCHAR2(500 CHAR),
    "S3_KEY"             VARCHAR2(1000 CHAR),
    "S3_REGION"          VARCHAR2(100 CHAR),
    -- Human-readable notes / description supplied by caller
    "DOCUMENT_NOTES"     CLOB,
    "UPLOAD_STATUS"      VARCHAR2(20 CHAR)   NOT NULL DEFAULT 'UPLOADED'
                            CONSTRAINT chk_doc_status CHECK ("UPLOAD_STATUS" IN ('UPLOADED','FAILED','DELETED')),
    "UPLOADED_BY"        VARCHAR2(200 CHAR),
    "CREATED_AT"         TIMESTAMP           NOT NULL DEFAULT SYSTIMESTAMP,
    "UPDATED_AT"         TIMESTAMP           NOT NULL DEFAULT SYSTIMESTAMP
);

COMMENT ON TABLE  "DOCUMENT_UPLOADS"              IS 'Stores uploaded case documents (PDFs, images, Office files).';
COMMENT ON COLUMN "DOCUMENT_UPLOADS"."DOCUMENT_ID"       IS 'Auto-generated surrogate key.';
COMMENT ON COLUMN "DOCUMENT_UPLOADS"."AWF_CASE_REF"      IS 'Optional case reference that owns this document.';
COMMENT ON COLUMN "DOCUMENT_UPLOADS"."ORIGINAL_FILENAME" IS 'Original filename as submitted by the browser.';
COMMENT ON COLUMN "DOCUMENT_UPLOADS"."SAFE_FILENAME"     IS 'Sanitised filename safe for filesystem / S3 keys.';
COMMENT ON COLUMN "DOCUMENT_UPLOADS"."CONTENT_TYPE"      IS 'MIME type declared by the client (validated server-side).';
COMMENT ON COLUMN "DOCUMENT_UPLOADS"."FILE_EXTENSION"    IS 'Normalised lowercase extension (.pdf, .docx, …).';
COMMENT ON COLUMN "DOCUMENT_UPLOADS"."FILE_SIZE_BYTES"   IS 'Exact size in bytes after reading the upload stream.';
COMMENT ON COLUMN "DOCUMENT_UPLOADS"."CHECKSUM_SHA256"   IS 'SHA-256 hex digest of the raw file bytes.';
COMMENT ON COLUMN "DOCUMENT_UPLOADS"."STORAGE_TYPE"      IS 'ORACLE_BLOB: content in FILE_CONTENT. S3: content in bucket. BOTH: mirrored.';
COMMENT ON COLUMN "DOCUMENT_UPLOADS"."FILE_CONTENT"      IS 'Binary file content for ORACLE_BLOB storage.';
COMMENT ON COLUMN "DOCUMENT_UPLOADS"."S3_BUCKET"         IS 'S3/ECS bucket name when STORAGE_TYPE is S3 or BOTH.';
COMMENT ON COLUMN "DOCUMENT_UPLOADS"."S3_KEY"            IS 'S3 object key when STORAGE_TYPE is S3 or BOTH.';
COMMENT ON COLUMN "DOCUMENT_UPLOADS"."DOCUMENT_NOTES"    IS 'Optional analyst notes / extracted text (CLOB).';
COMMENT ON COLUMN "DOCUMENT_UPLOADS"."UPLOADED_BY"       IS 'LAN ID or system identifier of the uploader.';

-- Indexes for common query patterns
CREATE INDEX "idx_doc_case_ref"  ON "DOCUMENT_UPLOADS" ("AWF_CASE_REF");
CREATE INDEX "idx_doc_uploaded_by" ON "DOCUMENT_UPLOADS" ("UPLOADED_BY");
CREATE INDEX "idx_doc_status"    ON "DOCUMENT_UPLOADS" ("UPLOAD_STATUS");
CREATE INDEX "idx_doc_created"   ON "DOCUMENT_UPLOADS" ("CREATED_AT" DESC);

-- Audit / download log
CREATE TABLE "DOCUMENT_ACCESS_LOG" (
    "LOG_ID"          NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    "DOCUMENT_ID"     NUMBER         NOT NULL
                        CONSTRAINT fk_log_doc REFERENCES "DOCUMENT_UPLOADS" ("DOCUMENT_ID") ON DELETE CASCADE,
    "ACCESS_TYPE"     VARCHAR2(20 CHAR) NOT NULL
                        CONSTRAINT chk_access_type CHECK ("ACCESS_TYPE" IN ('DOWNLOAD','DELETE','VIEW')),
    "ACCESSED_BY"     VARCHAR2(200 CHAR),
    "IP_ADDRESS"      VARCHAR2(45  CHAR),
    "ACCESSED_AT"     TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL
);

COMMENT ON TABLE  "DOCUMENT_ACCESS_LOG" IS 'Audit trail for document downloads and deletes.';

CREATE INDEX "idx_log_doc_id"   ON "DOCUMENT_ACCESS_LOG" ("DOCUMENT_ID");
CREATE INDEX "idx_log_accessed" ON "DOCUMENT_ACCESS_LOG" ("ACCESSED_AT" DESC);
