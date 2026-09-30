"""SQL constants for the document upload service."""

INSERT_DOCUMENT = """
    INSERT INTO {p}"DOCUMENT_UPLOADS"
        ("AWF_CASE_REF", "ORIGINAL_FILENAME", "SAFE_FILENAME",
         "CONTENT_TYPE", "FILE_EXTENSION", "FILE_SIZE_BYTES",
         "CHECKSUM_SHA256", "STORAGE_TYPE",
         "FILE_CONTENT", "S3_BUCKET", "S3_KEY", "S3_REGION",
         "DOCUMENT_NOTES", "UPLOAD_STATUS", "UPLOADED_BY",
         "CREATED_AT", "UPDATED_AT")
    VALUES
        (:awf_case_ref, :original_filename, :safe_filename,
         :content_type, :file_extension, :file_size_bytes,
         :checksum_sha256, :storage_type,
         :file_content, :s3_bucket, :s3_key, :s3_region,
         :document_notes, 'UPLOADED', :uploaded_by,
         SYSTIMESTAMP, SYSTIMESTAMP)
    RETURNING "DOCUMENT_ID", "CREATED_AT" INTO :out_document_id, :out_created_at
"""

FETCH_DOCUMENT_BY_ID = """
    SELECT
        d."DOCUMENT_ID",
        d."AWF_CASE_REF",
        d."ORIGINAL_FILENAME",
        d."CONTENT_TYPE",
        d."FILE_EXTENSION",
        d."FILE_SIZE_BYTES",
        d."CHECKSUM_SHA256",
        d."STORAGE_TYPE",
        d."FILE_CONTENT",
        d."S3_BUCKET",
        d."S3_KEY",
        d."DOCUMENT_NOTES",
        d."UPLOAD_STATUS",
        d."UPLOADED_BY",
        TO_CHAR(d."CREATED_AT", 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS "CREATED_AT"
      FROM {p}"DOCUMENT_UPLOADS" d
     WHERE d."DOCUMENT_ID" = :document_id
       AND d."UPLOAD_STATUS" != 'DELETED'
"""

FETCH_DOCUMENTS_BY_CASE = """
    SELECT
        d."DOCUMENT_ID",
        d."AWF_CASE_REF",
        d."ORIGINAL_FILENAME",
        d."CONTENT_TYPE",
        d."FILE_EXTENSION",
        d."FILE_SIZE_BYTES",
        d."CHECKSUM_SHA256",
        d."STORAGE_TYPE",
        d."S3_BUCKET",
        d."S3_KEY",
        d."UPLOAD_STATUS",
        d."UPLOADED_BY",
        TO_CHAR(d."CREATED_AT", 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS "CREATED_AT"
      FROM {p}"DOCUMENT_UPLOADS" d
     WHERE d."AWF_CASE_REF" = :awf_case_ref
       AND d."UPLOAD_STATUS" != 'DELETED'
     ORDER BY d."CREATED_AT" DESC
"""

FETCH_ALL_DOCUMENTS = """
    SELECT
        d."DOCUMENT_ID",
        d."AWF_CASE_REF",
        d."ORIGINAL_FILENAME",
        d."CONTENT_TYPE",
        d."FILE_EXTENSION",
        d."FILE_SIZE_BYTES",
        d."CHECKSUM_SHA256",
        d."STORAGE_TYPE",
        d."S3_BUCKET",
        d."S3_KEY",
        d."UPLOAD_STATUS",
        d."UPLOADED_BY",
        TO_CHAR(d."CREATED_AT", 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS "CREATED_AT"
      FROM {p}"DOCUMENT_UPLOADS" d
     WHERE d."UPLOAD_STATUS" != 'DELETED'
     ORDER BY d."CREATED_AT" DESC
"""

SOFT_DELETE_DOCUMENT = """
    UPDATE {p}"DOCUMENT_UPLOADS"
       SET "UPLOAD_STATUS" = 'DELETED',
           "UPDATED_AT"    = SYSTIMESTAMP
     WHERE "DOCUMENT_ID" = :document_id
       AND "UPLOAD_STATUS" != 'DELETED'
"""

INSERT_ACCESS_LOG = """
    INSERT INTO {p}"DOCUMENT_ACCESS_LOG"
        ("DOCUMENT_ID", "ACCESS_TYPE", "ACCESSED_BY", "IP_ADDRESS", "ACCESSED_AT")
    VALUES
        (:document_id, :access_type, :accessed_by, :ip_address, SYSTIMESTAMP)
"""
