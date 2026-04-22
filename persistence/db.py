"""DB connection helpers. Wraps the legacy `db_utils` DSN resolution.

Deliberately thin: the repositories consume raw connections so tests can
inject a sqlite / in-memory fake without needing a DB server.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator, Optional

try:
    import psycopg2  # type: ignore
    from psycopg2.extensions import connection as PGConnection  # type: ignore
except Exception:  # pragma: no cover
    psycopg2 = None  # type: ignore
    PGConnection = object  # type: ignore

from core.logging import get_logger

logger = get_logger(__name__)


def get_dsn() -> Optional[str]:
    return os.getenv("DATABASE_URL")


@contextmanager
def get_connection() -> Iterator[PGConnection]:
    dsn = get_dsn()
    if not dsn or psycopg2 is None:
        raise RuntimeError("DATABASE_URL not set or psycopg2 unavailable")
    conn = psycopg2.connect(dsn)
    try:
        yield conn
    finally:
        conn.close()


def init_persistence() -> None:
    """Idempotently create both legacy and new tables."""
    from persistence.schema import ensure_schema
    try:
        import db_utils  # legacy
        db_utils.init_db()
    except Exception as e:  # pragma: no cover
        logger.warning("Legacy db_utils.init_db failed: %s", e)

    with get_connection() as conn:
        ensure_schema(conn)
