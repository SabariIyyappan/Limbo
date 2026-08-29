"""Internal state: SQLite with explicit transaction control.

The connection is opened with isolation_level=None so Python never issues
an implicit COMMIT. The transaction is begun once at the start of a run and
stays open across every internal write, so Phase 4's rollback is a database
primitive rather than custom undo code.
"""
import os
import sqlite3

# Each world gets its own file. The unprotected side runs a second tool server
# with LIMBO_DB pointed elsewhere, so the two never share a transaction.
_DATA = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
DB_PATH = os.environ.get("LIMBO_DB", os.path.join(_DATA, "limbo.db"))

_conn: sqlite3.Connection | None = None


def conn() -> sqlite3.Connection:
    """Process-wide connection with autocommit disabled."""
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, isolation_level=None, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute(
            """CREATE TABLE IF NOT EXISTS vendors (
                   id      INTEGER PRIMARY KEY AUTOINCREMENT,
                   name    TEXT NOT NULL,
                   tax_id  TEXT NOT NULL,
                   contact TEXT
               )"""
        )
    return _conn


def in_transaction() -> bool:
    return conn().in_transaction


def begin() -> None:
    """Open a transaction if one isn't already open."""
    if not in_transaction():
        conn().execute("BEGIN")


def commit() -> None:
    if in_transaction():
        conn().execute("COMMIT")


def rollback() -> None:
    if in_transaction():
        conn().execute("ROLLBACK")


def insert_vendor(name: str, tax_id: str, contact: str | None = None) -> int:
    cur = conn().execute(
        "INSERT INTO vendors (name, tax_id, contact) VALUES (?, ?, ?)",
        (name, tax_id, contact),
    )
    return cur.lastrowid


def list_vendors() -> list[dict]:
    return [dict(r) for r in conn().execute("SELECT * FROM vendors ORDER BY id")]


def reset() -> None:
    """Drop all rows. Used between demo takes."""
    rollback()
    conn().execute("DELETE FROM vendors")
    conn().execute("DELETE FROM sqlite_sequence WHERE name='vendors'")
