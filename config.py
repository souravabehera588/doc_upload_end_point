"""Application settings — loaded from environment / .env file."""

from functools import lru_cache
from typing import List, Set

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # App
    app_env:       str = "development"
    app_log_level: str = "INFO"
    app_title:     str = "Document Upload Service"
    app_version:   str = "1.0.0"

    # Oracle (same env vars as gctst-backend-new)
    oracle_host:         str = "localhost"
    oracle_port:         int = 1521
    oracle_service_name: str = "GCTST"
    oracle_work_user:    str = ""
    oracle_userpwd:      str = ""
    oracle_schema_user:  str = ""
    oracle_pool_min:     int = 1
    oracle_pool_max:     int = 8
    oracle_pool_timeout: int = 60

    # AWS S3 (optional — leave empty to disable S3 storage)
    aws_access_key_id:     str = ""
    aws_secret_access_key: str = ""
    aws_region:            str = "ap-southeast-1"
    s3_bucket:             str = ""
    s3_key_prefix:         str = "documents/"
    s3_endpoint_url:       str = ""    # set for MinIO / ECS; leave empty for real AWS

    # Upload limits
    max_files_per_request:   int   = 10
    max_file_size_mb:        float = 50.0    # per file
    max_total_size_mb:       float = 200.0   # per request
    max_filename_length:     int   = 255

    # Allowed file types — extension→MIME mappings
    # The server validates BOTH extension AND magic bytes.
    allowed_extensions: str = "pdf,docx,doc,xlsx,xls,pptx,ppt,png,jpeg,jpg,gif,tiff,bmp,txt,csv,zip"

    # Storage strategy
    # 'local'   → filesystem binary + SQLite metadata (no Oracle or S3 required)
    # 'minio'   → MinIO object storage + SQLite metadata (no Oracle required)
    # 'oracle'  → always store in Oracle BLOB
    # 's3'      → always store in S3 (Oracle keeps metadata only)
    # 'both'    → store in both (Oracle primary, S3 mirror)
    # 'auto'    → Oracle for small files (≤ auto_blob_threshold_mb), S3 for large
    storage_strategy:       str   = "local"
    auto_blob_threshold_mb: float = 10.0

    # Local storage (used when storage_strategy = 'local')
    local_storage_path: str = "uploads/files"
    local_db_path:      str = "uploads/documents.db"

    # MinIO (used when storage_strategy = 'minio')
    minio_endpoint_url: str = "http://localhost:9000"
    minio_access_key:   str = "minioadmin"
    minio_secret_key:   str = "minioadmin"
    minio_bucket:       str = "documents"
    minio_key_prefix:   str = "documents/"
    minio_secure:       bool = False
    minio_db_path:      str = "uploads/minio_documents.db"

    @property
    def max_file_size_bytes(self) -> int:
        return int(self.max_file_size_mb * 1024 * 1024)

    @property
    def max_total_size_bytes(self) -> int:
        return int(self.max_total_size_mb * 1024 * 1024)

    @property
    def auto_blob_threshold_bytes(self) -> int:
        return int(self.auto_blob_threshold_mb * 1024 * 1024)

    @property
    def allowed_extension_set(self) -> Set[str]:
        return {ext.strip().lower().lstrip(".") for ext in self.allowed_extensions.split(",")}

    @property
    def s3_enabled(self) -> bool:
        return bool(self.s3_bucket and self.aws_access_key_id)


@lru_cache
def get_settings() -> Settings:
    return Settings()
