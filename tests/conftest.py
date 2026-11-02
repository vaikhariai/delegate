from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "api"))
sys.path.insert(0, str(ROOT))

from delegate.clock import Clock, FixedClock  # noqa: E402
from delegate.db import embedded_url, make_engine, migrate  # noqa: E402

T0 = dt.datetime(2026, 11, 3, 10, 0, tzinfo=dt.timezone.utc)


@pytest.fixture(scope="session")
def pg_admin_url() -> str:
    url = os.environ.get("DELEGATE_TEST_DATABASE_URL")
    if url:
        return url
    return embedded_url(tempfile.mkdtemp(prefix="delegate-test-pg-"))


@pytest.fixture()
def engine(pg_admin_url: str) -> Iterator[Engine]:
    name = "t_" + uuid.uuid4().hex[:12]
    admin = create_engine(pg_admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    url = pg_admin_url.replace("/postgres?", f"/{name}?") if "/postgres?" in pg_admin_url else pg_admin_url.rsplit("/", 1)[0] + "/" + name
    eng = make_engine(url, "", pool_size=12)
    migrate(eng)
    try:
        yield eng
    finally:
        eng.dispose()
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture()
def clock() -> FixedClock:
    return FixedClock(T0)


@pytest.fixture()
def real_clock() -> Clock:
    return Clock()
