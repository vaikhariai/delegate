"""PostgreSQL access: engine creation (embedded pgserver when no DATABASE_URL), migrations, small helpers.

SQL is written by hand with bound parameters. Every query that reads session data filters by session_id.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine, create_engine, event, text

ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS = ROOT / "migrations"

_servers: dict[str, Any] = {}


def embedded_url(pgdata_dir: str) -> str:
    """Start (or reuse) an embedded PostgreSQL under ``pgdata_dir`` and return a SQLAlchemy URL."""
    import pgserver

    path = str(Path(pgdata_dir).resolve())
    Path(path).mkdir(parents=True, exist_ok=True)
    if path not in _servers:
        _servers[path] = pgserver.get_server(path, cleanup_mode="stop")  # type: ignore[attr-defined]
    uri: str = _servers[path].get_uri()
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


def make_engine(database_url: str, pgdata_dir: str, pool_size: int = 10) -> Engine:
    url = database_url or embedded_url(pgdata_dir)
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    engine = create_engine(url, pool_size=pool_size, max_overflow=10, pool_pre_ping=True, future=True)

    @event.listens_for(engine, "connect")
    def _utc(dbapi_conn: Any, _rec: Any) -> None:
        with dbapi_conn.cursor() as cur:
            cur.execute("SET TIME ZONE 'UTC'")
        dbapi_conn.commit()

    return engine


def migrate(engine: Engine) -> list[str]:
    applied: list[str] = []
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(424242)"))
        conn.execute(text("CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"))
        done = {r[0] for r in conn.execute(text("SELECT name FROM schema_migrations"))}
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name in done:
                continue
            conn.exec_driver_sql(path.read_text())
            conn.execute(text("INSERT INTO schema_migrations (name) VALUES (:n)"), {"n": path.name})
            applied.append(path.name)
    return applied


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def rows(conn: Connection, sql: str, **params: Any) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(text(sql), _adapt(params)).mappings()]


def one(conn: Connection, sql: str, **params: Any) -> dict[str, Any] | None:
    r = conn.execute(text(sql), _adapt(params)).mappings().first()
    return dict(r) if r is not None else None


def exe(conn: Connection, sql: str, **params: Any) -> int:
    return int(conn.execute(text(sql), _adapt(params)).rowcount)


def _adapt(params: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in params.items():
        out[k] = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
    return out


@contextmanager
def tx(engine: Engine) -> Iterator[Connection]:
    with engine.begin() as conn:
        yield conn
