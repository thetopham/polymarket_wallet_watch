from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SCHEMA_PATH = PACKAGE_ROOT / "sql" / "schema.sql"


def connect(path: str | Path) -> sqlite3.Connection:
    db_path = Path(path)
    if str(path) != ":memory:":
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def initialize_schema(conn: sqlite3.Connection, schema_path: str | Path | None = None) -> None:
    schema = Path(schema_path or DEFAULT_SCHEMA_PATH).read_text()
    conn.executescript(schema)
    conn.commit()


def insert_dict(conn: sqlite3.Connection, table: str, row: dict[str, Any], *, or_ignore: bool = False) -> int:
    clean = {k: v for k, v in row.items() if v is not None}
    if not clean:
        raise ValueError("cannot insert empty row")
    columns = ", ".join(clean.keys())
    placeholders = ", ".join(["?"] * len(clean))
    verb = "INSERT OR IGNORE" if or_ignore else "INSERT"
    cur = conn.execute(f"{verb} INTO {table} ({columns}) VALUES ({placeholders})", tuple(clean.values()))
    conn.commit()
    return int(cur.lastrowid or 0)


def upsert_wallet(conn: sqlite3.Connection, wallet_address: str, label: str | None = None, enabled: bool = True) -> None:
    conn.execute(
        """
        INSERT INTO wallets(wallet_address, label, watch_enabled)
        VALUES(?, ?, ?)
        ON CONFLICT(wallet_address) DO UPDATE SET
            label=COALESCE(excluded.label, wallets.label),
            watch_enabled=excluded.watch_enabled
        """,
        (wallet_address.lower(), label, 1 if enabled else 0),
    )
    conn.commit()
