"""
database.py — SQLite schema initialisation and connection helpers.

Calling ``init_db()`` on server startup creates the ``fileshare.db`` file
(if it does not already exist) and ensures both the ``users`` and ``files``
tables are present.  Every other module should obtain a connection through
``get_connection()`` so that WAL mode, foreign-key enforcement, and
row-factory settings are applied consistently.
"""

import sqlite3
from config import DATABASE_PATH


def get_connection() -> sqlite3.Connection:
    """Return a new SQLite connection with recommended pragmas enabled.

    * ``PRAGMA journal_mode = WAL`` — allows concurrent readers while a
      write is in progress (important when Flask serves multiple requests).
    * ``PRAGMA foreign_keys = ON`` — enforces FK constraints at runtime.
    * ``row_factory = sqlite3.Row`` — rows behave like dicts (access by
      column name).
    """
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """Create the ``users`` and ``files`` tables if they do not exist.

    This function is *idempotent*: it can be called on every server startup
    without side-effects on an already-initialised database.
    """
    conn = get_connection()
    try:
        conn.executescript(
            """
            -- ---------------------------------------------------------------
            -- users
            -- ---------------------------------------------------------------
            CREATE TABLE IF NOT EXISTS users (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                username          TEXT    NOT NULL UNIQUE,
                email             TEXT    NOT NULL UNIQUE,
                password_hash     TEXT    NOT NULL,
                quota_limit_bytes INTEGER NOT NULL DEFAULT 52428800,
                quota_used_bytes  INTEGER NOT NULL DEFAULT 0,
                created_at        TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            -- ---------------------------------------------------------------
            -- files
            -- ---------------------------------------------------------------
            CREATE TABLE IF NOT EXISTS files (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                filename        TEXT    NOT NULL,
                original_name   TEXT    NOT NULL,
                file_type       TEXT    NOT NULL,
                file_size_bytes INTEGER NOT NULL,
                uploaded_at     TEXT    NOT NULL DEFAULT (datetime('now')),
                owner_id        INTEGER NOT NULL,
                FOREIGN KEY (owner_id) REFERENCES users (id)
                    ON DELETE CASCADE
            );
            """
        )
        conn.commit()
    finally:
        conn.close()
