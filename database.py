"""
database.py — SQLite schema initialisation, connection helpers, and file metadata CRUD.

Calling ``init_db()`` on server startup creates the ``fileshare.db`` file
(if it does not already exist) and ensures both the ``users`` and ``files``
tables are present.  Every other module should obtain a connection through
``get_connection()`` so that WAL mode, foreign-key enforcement, and
row-factory settings are applied consistently.

NOTE: ``get_connection()`` reads ``config.DATABASE_PATH`` at *call time*
(not at import time) so that test suites can override the path before any
connection is opened.
"""

import sqlite3
from datetime import datetime, timezone
from typing import Any

import config


def get_connection() -> sqlite3.Connection:
    """Return a new SQLite connection with recommended pragmas enabled.

    * ``PRAGMA journal_mode = WAL`` — allows concurrent readers while a
      write is in progress (important when Flask serves multiple requests).
    * ``PRAGMA foreign_keys = ON`` — enforces FK constraints at runtime.
    * ``row_factory = sqlite3.Row`` — rows behave like dicts (access by
      column name).
    """
    # Read at call-time so tests can override config.DATABASE_PATH
    conn = sqlite3.connect(config.DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def normalize_timestamp(ts_str: str) -> str:
    """Normalize timestamp string into standardized UTC ISO 8601 YYYY-MM-DDTHH:MM:SSZ format."""
    ts_str = ts_str.strip()
    # If already in the standard format YYYY-MM-DDTHH:MM:SSZ
    if len(ts_str) == 20 and ts_str.endswith('Z') and 'T' in ts_str:
        return ts_str

    # If it is in 'YYYY-MM-DD HH:MM:SS' format (SQLite default datetime('now'))
    if ' ' in ts_str and 'T' not in ts_str:
        parts = ts_str.split(' ')
        if len(parts) == 2:
            return f"{parts[0]}T{parts[1]}Z"

    # Otherwise parse as general ISO format
    try:
        val = ts_str
        if val.endswith('Z'):
            val = val[:-1] + '+00:00'
        dt = datetime.fromisoformat(val)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc)
        else:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.strftime('%Y-%m-%dT%H:%M:%SZ')
    except Exception:
        return ts_str


def normalize_existing_timestamps() -> None:
    """Find all users and files rows and update their timestamps to standard UTC ISO 8601 format."""
    conn = get_connection()
    try:
        # Normalize users.created_at
        users = conn.execute("SELECT id, created_at FROM users").fetchall()
        with conn:
            for user in users:
                raw_ts = user["created_at"]
                if raw_ts:
                    norm = normalize_timestamp(raw_ts)
                    if norm != raw_ts:
                        conn.execute(
                            "UPDATE users SET created_at = ? WHERE id = ?",
                            (norm, user["id"])
                        )

        # Normalize files.uploaded_at
        files = conn.execute("SELECT id, uploaded_at FROM files").fetchall()
        with conn:
            for f in files:
                raw_ts = f["uploaded_at"]
                if raw_ts:
                    norm = normalize_timestamp(raw_ts)
                    if norm != raw_ts:
                        conn.execute(
                            "UPDATE files SET uploaded_at = ? WHERE id = ?",
                            (norm, f["id"])
                        )
    except sqlite3.OperationalError:
        # Tables might not exist yet if called before tables are created
        pass
    finally:
        conn.close()


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
                created_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
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
                uploaded_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                owner_id        INTEGER NOT NULL,
                FOREIGN KEY (owner_id) REFERENCES users (id)
                    ON DELETE CASCADE
            );
            """
        )
        conn.commit()
    finally:
        conn.close()

    # Run database migration step to standardize existing timestamps
    normalize_existing_timestamps()


# ── File metadata helpers ─────────────────────────────────────────────────

def add_file(filename: str, original_name: str, file_type: str,
             file_size_bytes: int, owner_id: int) -> None:
    """Insert a new file record into the ``files`` table."""
    conn = get_connection()
    uploaded_at = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO files
                    (filename, original_name, file_type, file_size_bytes, uploaded_at, owner_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (filename, original_name, file_type, file_size_bytes, uploaded_at, owner_id),
            )
    finally:
        conn.close()


def get_file_by_name(filename: str) -> dict[str, Any] | None:
    """Return the file row for *filename*, or ``None`` if not found."""
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT id, filename, original_name, file_type,
                   file_size_bytes, uploaded_at, owner_id
            FROM files WHERE filename = ?
            """,
            (filename,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def delete_file(filename: str) -> None:
    """Delete the file record for *filename* from the database."""
    conn = get_connection()
    try:
        with conn:
            conn.execute("DELETE FROM files WHERE filename = ?", (filename,))
    finally:
        conn.close()


def delete_file_and_decrement_quota(filename: str) -> None:
    """Delete the file record and decrement the owner's quota in a single transaction."""
    conn = get_connection()
    try:
        with conn:
            row = conn.execute(
                "SELECT owner_id, file_size_bytes FROM files WHERE filename = ?",
                (filename,)
            ).fetchone()
            if row:
                owner_id = row["owner_id"]
                file_size_bytes = row["file_size_bytes"]
                
                # Delete the file record
                conn.execute("DELETE FROM files WHERE filename = ?", (filename,))
                
                # Decrement quota_used_bytes for the user
                user = conn.execute(
                    "SELECT quota_used_bytes FROM users WHERE id = ?",
                    (owner_id,)
                ).fetchone()
                if user:
                    new_quota = max(0, user["quota_used_bytes"] - file_size_bytes)
                    conn.execute(
                        "UPDATE users SET quota_used_bytes = ? WHERE id = ?",
                        (new_quota, owner_id)
                    )
    finally:
        conn.close()


def rename_file(old_filename: str, new_filename: str) -> None:
    """Rename a file record in the database."""
    conn = get_connection()
    try:
        with conn:
            conn.execute(
                "UPDATE files SET filename = ?, original_name = ? WHERE filename = ?",
                (new_filename, new_filename, old_filename),
            )
    finally:
        conn.close()


def get_all_files() -> list[dict[str, Any]]:
    """Return all files joined with their owner's username, newest first."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT f.id, f.filename, f.original_name, f.file_type,
                   f.file_size_bytes, f.uploaded_at, f.owner_id,
                   u.username AS owner_username
            FROM files f
            JOIN users u ON f.owner_id = u.id
            ORDER BY f.uploaded_at DESC, f.id DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()
