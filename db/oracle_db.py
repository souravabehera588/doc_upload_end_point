"""Oracle connection pool and base DAO — same pattern as gctst-backend-new."""

from __future__ import annotations

import contextlib
import logging
import os
import threading
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)


class OracleDBError(Exception):
    """Raised when the Oracle layer cannot serve a request."""


_pool = None
_pool_lock = threading.Lock()


def schema_prefix() -> str:
    schema = os.environ.get("ORACLE_SCHEMA_USER", "").strip()
    return f'"{schema}".' if schema else ""


def _build_pool():
    try:
        import oracledb
    except ImportError as exc:
        raise OracleDBError("oracledb package not installed") from exc

    try:
        oracledb.defaults.fetch_lobs = False
    except Exception:
        pass

    host     = os.environ.get("ORACLE_HOST", "localhost")
    port     = int(os.environ.get("ORACLE_PORT", "1521"))
    service  = os.environ.get("ORACLE_SERVICE_NAME", "GCTST")
    user     = os.environ.get("ORACLE_WORK_USER", "")
    password = os.environ.get("ORACLE_USERPWD", "")
    dsn      = f"{host}:{port}/{service}"

    try:
        return oracledb.create_pool(
            user=user,
            password=password,
            dsn=dsn,
            min=int(os.environ.get("ORACLE_POOL_MIN", "1")),
            max=int(os.environ.get("ORACLE_POOL_MAX", "8")),
            increment=1,
            getmode=oracledb.POOL_GETMODE_WAIT,
            timeout=int(os.environ.get("ORACLE_POOL_TIMEOUT", "60")),
        )
    except Exception as exc:
        raise OracleDBError(f"Oracle pool creation failed ({dsn} as {user}): {exc}") from exc


def get_pool():
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = _build_pool()
    return _pool


def reset_pool() -> None:
    global _pool
    with _pool_lock:
        if _pool is not None:
            try:
                _pool.close(force=True)
            except Exception:
                pass
            _pool = None


def rows_to_dicts(cur) -> List[Dict[str, Any]]:
    cols = [d[0].lower() for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def row_to_dict(cur) -> Optional[Dict[str, Any]]:
    row = cur.fetchone()
    if row is None:
        return None
    cols = [d[0].lower() for d in cur.description]
    return dict(zip(cols, row))


class OracleDB:
    """Universal base accessor over the shared Oracle pool."""

    error_type: type = OracleDBError

    def __init__(self) -> None:
        self._prefix = schema_prefix()

    def sql(self, template: str) -> str:
        return template.format(p=self._prefix)

    def _fail(self, what: str, exc: Exception) -> OracleDBError:
        if isinstance(exc, OracleDBError):
            return exc
        return self.error_type(f"{what} failed: {exc}")

    def _acquire(self):
        try:
            return get_pool().acquire()
        except OracleDBError:
            raise
        except Exception as exc:
            raise self.error_type(f"Oracle connection acquire failed: {exc}") from exc

    @contextlib.contextmanager
    def connection(self):
        conn = self._acquire()
        try:
            yield conn
        finally:
            with contextlib.suppress(Exception):
                conn.close()

    def fetch_all(
        self,
        query: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        conn=None,
    ) -> List[Dict[str, Any]]:
        try:
            if conn is not None:
                with conn.cursor() as cur:
                    cur.execute(self.sql(query), params or {})
                    return rows_to_dicts(cur)
            with self._acquire() as owned:
                with owned.cursor() as cur:
                    cur.execute(self.sql(query), params or {})
                    return rows_to_dicts(cur)
        except OracleDBError:
            raise
        except Exception as exc:
            raise self._fail("fetch_all", exc) from exc

    def fetch_one(
        self,
        query: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        conn=None,
    ) -> Optional[Dict[str, Any]]:
        try:
            if conn is not None:
                with conn.cursor() as cur:
                    cur.execute(self.sql(query), params or {})
                    return row_to_dict(cur)
            with self._acquire() as owned:
                with owned.cursor() as cur:
                    cur.execute(self.sql(query), params or {})
                    return row_to_dict(cur)
        except OracleDBError:
            raise
        except Exception as exc:
            raise self._fail("fetch_one", exc) from exc

    def execute(
        self,
        query: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        conn=None,
    ) -> int:
        try:
            if conn is not None:
                with conn.cursor() as cur:
                    cur.execute(self.sql(query), params or {})
                    return cur.rowcount
            with self._acquire() as owned:
                with owned.cursor() as cur:
                    cur.execute(self.sql(query), params or {})
                    owned.commit()
                    return cur.rowcount
        except OracleDBError:
            raise
        except Exception as exc:
            raise self._fail("execute", exc) from exc

    def execute_returning(
        self,
        query: str,
        params: Dict[str, Any],
        *,
        out_vars: Dict[str, Any],
        conn=None,
    ) -> Dict[str, Any]:
        """Execute INSERT … RETURNING … INTO :out_* and return the out-var values."""
        try:
            import oracledb

            def _run(cur):
                bound = {**params}
                resolved_out = {}
                for name, db_type in out_vars.items():
                    v = cur.var(db_type)
                    bound[name] = v
                    resolved_out[name] = v
                cur.execute(self.sql(query), bound)
                return {name: v.getvalue() for name, v in resolved_out.items()}

            if conn is not None:
                with conn.cursor() as cur:
                    return _run(cur)
            with self._acquire() as owned:
                with owned.cursor() as cur:
                    result = _run(cur)
                    owned.commit()
                    return result
        except OracleDBError:
            raise
        except Exception as exc:
            raise self._fail("execute_returning", exc) from exc
