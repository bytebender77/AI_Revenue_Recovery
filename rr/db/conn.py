from __future__ import annotations

import os
import pathlib

import psycopg

DEFAULT_PORT = os.environ.get("RR_DB_PORT", "5434")
DEFAULT_URL = f"postgresql://rr:rr@localhost:{DEFAULT_PORT}/rr"
SCHEMA = pathlib.Path(__file__).with_name("schema.sql")


def url() -> str:
    """DATABASE_URL wins; otherwise RR_DB_PORT (default 5434) against the compose db."""
    return os.environ.get("DATABASE_URL", DEFAULT_URL)


def connect() -> psycopg.Connection:
    return psycopg.connect(url(), autocommit=False)


def init_schema() -> None:
    """Destructive: drops and recreates. `make db-init`."""
    with connect() as conn:
        conn.execute(SCHEMA.read_text())
        conn.commit()


def ensure_schema() -> None:
    """Idempotent: create the schema only if it is not already there, so a clean
    clone runs `docker compose up` then `make run` with nothing in between."""
    with connect() as conn:
        exists = conn.execute("SELECT to_regclass('public.failure_event')").fetchone()[0]
        if exists is None:
            conn.execute(SCHEMA.read_text())
            conn.commit()
