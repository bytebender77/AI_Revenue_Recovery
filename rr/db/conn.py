from __future__ import annotations

import os
import pathlib

import psycopg

DEFAULT_URL = "postgresql://rr:rr@localhost:5434/rr"
SCHEMA = pathlib.Path(__file__).with_name("schema.sql")


def url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_URL)


def connect() -> psycopg.Connection:
    return psycopg.connect(url(), autocommit=False)


def init_schema() -> None:
    with connect() as conn:
        conn.execute(SCHEMA.read_text())
        conn.commit()
