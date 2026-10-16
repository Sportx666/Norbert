"""Database connection and migrations (Postgres / Supabase)."""
from __future__ import annotations

from contextlib import contextmanager
from importlib import resources

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

CONNECT_KWARGS = {
    "row_factory": dict_row,
    # Safe behind Supabase's poolers: no server-side prepared statements.
    "prepare_threshold": None,
}


def _configure(conn: psycopg.Connection) -> None:
    conn.execute("set time zone 'UTC'")
    conn.commit()


def make_pool(url: str, max_size: int = 4) -> ConnectionPool:
    pool = ConnectionPool(url, min_size=1, max_size=max_size, kwargs=CONNECT_KWARGS,
                          configure=_configure, open=False)
    pool.open(wait=True, timeout=30)
    return pool


def check_connection(url: str) -> str | None:
    """One quick direct connection, so a wrong URL fails in seconds with the server's own message."""
    try:
        with psycopg.connect(url, connect_timeout=10, **CONNECT_KWARGS) as conn:
            conn.execute("select 1")
    except Exception as e:  # noqa: BLE001 - shown to the user verbatim
        return str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
    return None


class Database:
    """Thin wrapper so callers write `with db.tx() as conn:` and get one transaction."""

    def __init__(self, url: str | None = None, pool: ConnectionPool | None = None):
        if pool is None and url is None:
            raise ValueError("need url or pool")
        self.pool = pool or make_pool(url)

    @contextmanager
    def tx(self):
        with self.pool.connection() as conn:
            with conn.transaction():
                yield conn

    def close(self):
        self.pool.close()


def migrate(conn: psycopg.Connection) -> list[str]:
    """Apply pending SQL migrations in order. Returns the versions applied."""
    conn.execute("create schema if not exists ft")
    conn.execute(
        "create table if not exists ft.schema_migrations (version text primary key, applied_at timestamptz not null default now())"
    )
    done = {r["version"] for r in conn.execute("select version from ft.schema_migrations").fetchall()}
    applied = []
    files = sorted(
        (f for f in resources.files("firstthings.migrations").iterdir() if f.name.endswith(".sql")),
        key=lambda f: f.name,
    )
    for f in files:
        version = f.name.split("_", 1)[0]
        if version in done:
            continue
        conn.execute(f.read_text(encoding="utf-8"))
        conn.execute("insert into ft.schema_migrations (version) values (%s)", (version,))
        applied.append(f.name)
    return applied
