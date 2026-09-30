"""FastAPI application — Document Upload Service."""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from config import get_settings
from routes.documents import router as documents_router
from schemas import ErrorCode, ErrorResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logger.info("Document Upload Service starting  env=%s", settings.app_env)
    logger.info(
        "Limits: files=%d  per_file=%s MB  total=%s MB  strategy=%s",
        settings.max_files_per_request,
        settings.max_file_size_mb,
        settings.max_total_size_mb,
        settings.storage_strategy,
    )
    logger.info("S3 enabled: %s  bucket=%s", settings.s3_enabled, settings.s3_bucket or "—")
    yield
    logger.info("Document Upload Service shutting down")
    try:
        from db.oracle_db import reset_pool
        reset_pool()
    except Exception:
        pass


# ── App factory ───────────────────────────────────────────────────────────────

def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title=settings.app_title,
        version=settings.app_version,
        description=(
            "Multi-file document upload service for GC TST. "
            "Accepts PDF, DOCX, XLSX, PNG, JPEG and other formats. "
            "Stores binary content in Oracle BLOB and/or AWS S3."
        ),
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    # ── CORS ─────────────────────────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],      # Tighten for production
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Request timing middleware ─────────────────────────────────────────────
    @app.middleware("http")
    async def add_request_timing(request: Request, call_next):
        t0       = time.perf_counter()
        response = await call_next(request)
        elapsed  = (time.perf_counter() - t0) * 1000
        response.headers["X-Response-Time-Ms"] = f"{elapsed:.1f}"
        logger.debug("%s %s → %d (%.1f ms)",
                     request.method, request.url.path, response.status_code, elapsed)
        return response

    # ── Global exception handlers ─────────────────────────────────────────────

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        """Convert Pydantic validation errors to a structured JSON response."""
        details = []
        for err in exc.errors():
            loc = " → ".join(str(l) for l in err.get("loc", []) if l != "body")
            details.append({"field": loc or "body", "message": err["msg"],
                            "type": err["type"]})
        logger.info("Request validation failed: %s %s — %d error(s)",
                    request.method, request.url.path, len(details))
        return JSONResponse(
            status_code=422,
            content={
                "error_code":    ErrorCode.INVALID_CONTENT_TYPE,
                "error_message": "Request validation failed.",
                "detail":        details,
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        """Catch-all: log and return 500 without leaking internals."""
        logger.exception("Unhandled exception for %s %s: %s",
                         request.method, request.url.path, exc)
        return JSONResponse(
            status_code=500,
            content={
                "error_code":    ErrorCode.INTERNAL_ERROR,
                "error_message": "An internal server error occurred.",
                "detail":        None,
            },
        )

    # ── Routes ────────────────────────────────────────────────────────────────
    app.include_router(documents_router, prefix="/api/v1")

    # ── Health check ──────────────────────────────────────────────────────────
    @app.get("/health", tags=["health"], include_in_schema=False)
    async def health():
        return {"status": "ok", "service": "doc-upload"}

    @app.get("/health/db", tags=["health"], include_in_schema=False)
    async def health_db():
        """Probe the configured storage backend."""
        cfg = get_settings()
        if cfg.storage_strategy == "local":
            try:
                import os
                from db.local_db import LocalDocumentDB
                db = LocalDocumentDB()
                db_ok   = os.path.isfile(cfg.local_db_path)
                dir_ok  = os.path.isdir(cfg.local_storage_path)
                if db_ok and dir_ok:
                    return {"status": "ok", "storage": "local",
                            "db": cfg.local_db_path, "files_dir": cfg.local_storage_path}
                return JSONResponse(status_code=503, content={
                    "status": "degraded", "storage": "local",
                    "db_exists": db_ok, "files_dir_exists": dir_ok,
                })
            except Exception as exc:
                logger.warning("Local storage health check failed: %s", exc)
                return JSONResponse(status_code=503, content={"status": "error", "storage": "local", "detail": str(exc)})
        elif cfg.storage_strategy == "minio":
            try:
                from db.minio_db import MinioDocumentDB
                db = MinioDocumentDB()
                db.ping()
                return {"status": "ok", "storage": "minio",
                        "endpoint": cfg.minio_endpoint_url, "bucket": cfg.minio_bucket}
            except Exception as exc:
                logger.warning("MinIO health check failed: %s", exc)
                return JSONResponse(status_code=503, content={"status": "error", "storage": "minio", "detail": str(exc)})
        else:
            try:
                from db.document_db import DocumentDB
                db     = DocumentDB()
                result = db.fetch_one("SELECT 1 AS ping FROM DUAL")
                if result and result.get("ping") == 1:
                    return {"status": "ok", "storage": "oracle"}
                return JSONResponse(status_code=503, content={"status": "degraded", "storage": "oracle"})
            except Exception as exc:
                logger.warning("Oracle health check failed: %s", exc)
                return JSONResponse(status_code=503, content={"status": "error", "storage": "oracle", "detail": str(exc)})

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8010, reload=True, log_level="info")
