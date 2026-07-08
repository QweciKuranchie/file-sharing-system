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
import uuid

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
    """Create the ``users``, ``folders``, ``files``, and ``file_shares`` tables if they do not exist.

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
            -- folders
            -- ---------------------------------------------------------------
            CREATE TABLE IF NOT EXISTS folders (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                name             TEXT    NOT NULL,
                owner_id         INTEGER NOT NULL,
                parent_folder_id INTEGER,
                created_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                FOREIGN KEY (owner_id) REFERENCES users (id) ON DELETE CASCADE,
                FOREIGN KEY (parent_folder_id) REFERENCES folders (id) ON DELETE CASCADE
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
                folder_id       INTEGER,
                visibility      TEXT    NOT NULL DEFAULT 'public',
                share_token     TEXT    UNIQUE,
                FOREIGN KEY (owner_id) REFERENCES users (id) ON DELETE CASCADE,
                FOREIGN KEY (folder_id) REFERENCES folders (id) ON DELETE SET NULL
            );

            -- ---------------------------------------------------------------
            -- file_shares
            -- ---------------------------------------------------------------
            CREATE TABLE IF NOT EXISTS file_shares (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id             INTEGER NOT NULL,
                shared_with_user_id INTEGER NOT NULL,
                FOREIGN KEY (file_id) REFERENCES files (id) ON DELETE CASCADE,
                FOREIGN KEY (shared_with_user_id) REFERENCES users (id) ON DELETE CASCADE,
                UNIQUE(file_id, shared_with_user_id)
            );
            """
        )
        conn.commit()

        # Idempotent schema migration for files table columns
        cursor = conn.execute("PRAGMA table_info(files)")
        columns = [row["name"] for row in cursor.fetchall()]
        
        with conn:
            if "folder_id" not in columns:
                conn.execute(
                    "ALTER TABLE files ADD COLUMN folder_id INTEGER REFERENCES folders(id) ON DELETE SET NULL"
                )
            if "visibility" not in columns:
                conn.execute(
                    "ALTER TABLE files ADD COLUMN visibility TEXT NOT NULL DEFAULT 'public'"
                )
            if "share_token" not in columns:
                conn.execute(
                    "ALTER TABLE files ADD COLUMN share_token TEXT"
                )
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_files_share_token ON files (share_token)"
                )
    finally:
        conn.close()

    # Run database migration step to standardize existing timestamps
    normalize_existing_timestamps()


# ── File metadata helpers ─────────────────────────────────────────────────

def add_file(filename: str, original_name: str, file_type: str,
             file_size_bytes: int, owner_id: int, folder_id: int | None = None,
             visibility: str = 'public', share_token: str | None = None) -> None:
    """Insert a new file record into the ``files`` table."""
    conn = get_connection()
    uploaded_at = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO files
                    (filename, original_name, file_type, file_size_bytes, uploaded_at,
                     owner_id, folder_id, visibility, share_token)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (filename, original_name, file_type, file_size_bytes, uploaded_at,
                 owner_id, folder_id, visibility, share_token),
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
                   file_size_bytes, uploaded_at, owner_id,
                   folder_id, visibility, share_token
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


def rename_file(old_filename: str, new_filename: str, new_original_name: str | None = None) -> None:
    """Rename a file record in the database.
    
    Parameters
    ----------
    old_filename : str
        The current stored filename (used to locate the row).
    new_filename : str
        The new stored filename (safe, on-disk name).
    new_original_name : str, optional
        The new display name to show users. If omitted, the existing
        original_name is preserved (recommended — don't overwrite display name
        with the sanitised stored name).
    """
    conn = get_connection()
    try:
        with conn:
            if new_original_name is not None:
                conn.execute(
                    "UPDATE files SET filename = ?, original_name = ? WHERE filename = ?",
                    (new_filename, new_original_name, old_filename),
                )
            else:
                # Preserve the existing original_name — only update the stored filename
                conn.execute(
                    "UPDATE files SET filename = ? WHERE filename = ?",
                    (new_filename, old_filename),
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
                   f.folder_id, f.visibility, f.share_token,
                   u.username AS owner_username
            FROM files f
            JOIN users u ON f.owner_id = u.id
            ORDER BY f.uploaded_at DESC, f.id DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_filtered_files(search: str | None = None, file_type: str | None = None) -> list[dict[str, Any]]:
    """Return filtered files joined with their owner's username, newest first."""
    conn = get_connection()
    try:
        query = """
            SELECT f.id, f.filename, f.original_name, f.file_type,
                   f.file_size_bytes, f.uploaded_at, f.owner_id,
                   f.folder_id, f.visibility, f.share_token,
                   u.username AS owner_username
            FROM files f
            JOIN users u ON f.owner_id = u.id
        """
        where_clauses = []
        params = []

        if search:
            where_clauses.append("(f.original_name LIKE ? OR f.filename LIKE ?)")
            params.extend([f"%{search}%", f"%{search}%"])

        if file_type and file_type.lower() != 'all':
            where_clauses.append("LOWER(f.file_type) = ?")
            params.append(file_type.lower())

        if where_clauses:
            query += " WHERE " + " AND ".join(where_clauses)

        query += " ORDER BY f.uploaded_at DESC, f.id DESC"
        
        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


# ── Folders & Sharing helpers ─────────────────────────────────────────────

def create_folder(name: str, owner_id: int, parent_folder_id: int | None = None) -> int:
    """Create a new folder and return its ID.
    
    Raises ValueError if name is empty or only whitespace.
    """
    if not name or not name.strip():
        raise ValueError("Folder name must not be empty")
    
    conn = get_connection()
    created_at = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    try:
        if parent_folder_id is not None:
            # Validate parent folder
            parent = conn.execute("SELECT id, owner_id FROM folders WHERE id = ?", (parent_folder_id,)).fetchone()
            if not parent:
                raise ValueError("Parent folder does not exist")
            if parent["owner_id"] != owner_id:
                raise ValueError("Parent folder is owned by another user")
        with conn:
            cursor = conn.execute(
                """
                INSERT INTO folders (name, owner_id, parent_folder_id, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (name.strip(), owner_id, parent_folder_id, created_at)
            )
            return cursor.lastrowid
    finally:
        conn.close()


def get_folders_for_user(owner_id: int, parent_folder_id: int | None = None) -> list[dict[str, Any]]:
    """Return all folders owned by owner_id under parent_folder_id."""
    conn = get_connection()
    try:
        if parent_folder_id is None:
            rows = conn.execute(
                """
                SELECT id, name, owner_id, parent_folder_id, created_at
                FROM folders
                WHERE owner_id = ? AND parent_folder_id IS NULL
                ORDER BY name ASC
                """,
                (owner_id,)
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, name, owner_id, parent_folder_id, created_at
                FROM folders
                WHERE owner_id = ? AND parent_folder_id = ?
                ORDER BY name ASC
                """,
                (owner_id, parent_folder_id)
            ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_subfolders(parent_folder_id: int | None = None) -> list[dict[str, Any]]:
    """Return all folders under parent_folder_id, including owner username, sorted by name."""
    conn = get_connection()
    try:
        if parent_folder_id is None:
            rows = conn.execute(
                """
                SELECT f.id, f.name, f.owner_id, f.parent_folder_id, f.created_at,
                       u.username AS owner_username
                FROM folders f
                JOIN users u ON f.owner_id = u.id
                WHERE f.parent_folder_id IS NULL
                ORDER BY f.name ASC
                """
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT f.id, f.name, f.owner_id, f.parent_folder_id, f.created_at,
                       u.username AS owner_username
                FROM folders f
                JOIN users u ON f.owner_id = u.id
                WHERE f.parent_folder_id = ?
                ORDER BY f.name ASC
                """,
                (parent_folder_id,)
            ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def delete_folder(folder_id: int, owner_id: int) -> None:
    """Delete a folder if owned by owner_id. Contained files' folder_id are set to NULL by ON DELETE SET NULL."""
    conn = get_connection()
    try:
        with conn:
            conn.execute(
                "DELETE FROM folders WHERE id = ? AND owner_id = ?",
                (folder_id, owner_id)
            )
    finally:
        conn.close()


def get_folder_by_id(folder_id: int) -> dict[str, Any] | None:
    """Return the folder row for the given ID, or None if not found."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, name, owner_id, parent_folder_id, created_at FROM folders WHERE id = ?",
            (folder_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_folder_path(folder_id: int) -> list[dict[str, Any]]:
    """Return the list of folders from the root (or top-most parent) down to folder_id."""
    path = []
    curr_id = folder_id
    for _ in range(100):
        folder = get_folder_by_id(curr_id)
        if not folder:
            break
        path.append(folder)
        if folder['parent_folder_id'] is None:
            break
        curr_id = folder['parent_folder_id']
    path.reverse()
    return path


def get_files_in_folder(folder_id: int | None, viewer_user_id: int) -> list[dict[str, Any]]:
    """Return all files in folder_id that are visible to viewer_user_id."""
    conn = get_connection()
    try:
        if folder_id is None:
            query = """
                SELECT f.id, f.filename, f.original_name, f.file_type,
                       f.file_size_bytes, f.uploaded_at, f.owner_id,
                       f.folder_id, f.visibility, f.share_token,
                       u.username AS owner_username
                FROM files f
                JOIN users u ON f.owner_id = u.id
                WHERE f.folder_id IS NULL AND (
                    f.owner_id = ?
                    OR f.visibility = 'public'
                    OR f.id IN (SELECT file_id FROM file_shares WHERE shared_with_user_id = ?)
                )
                ORDER BY f.uploaded_at DESC, f.id DESC
            """
            rows = conn.execute(query, (viewer_user_id, viewer_user_id)).fetchall()
        else:
            query = """
                SELECT f.id, f.filename, f.original_name, f.file_type,
                       f.file_size_bytes, f.uploaded_at, f.owner_id,
                       f.folder_id, f.visibility, f.share_token,
                       u.username AS owner_username
                FROM files f
                JOIN users u ON f.owner_id = u.id
                WHERE f.folder_id = ? AND (
                    f.owner_id = ?
                    OR f.visibility = 'public'
                    OR f.id IN (SELECT file_id FROM file_shares WHERE shared_with_user_id = ?)
                )
                ORDER BY f.uploaded_at DESC, f.id DESC
            """
            rows = conn.execute(query, (folder_id, viewer_user_id, viewer_user_id)).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def set_file_visibility(filename: str, visibility: str, owner_id: int) -> None:
    """Set the visibility of a file owned by owner_id."""
    if visibility not in ('private', 'public', 'shared'):
        raise ValueError("Visibility must be 'private', 'public', or 'shared'")
    
    conn = get_connection()
    try:
        with conn:
            cursor = conn.execute(
                "UPDATE files SET visibility = ? WHERE filename = ? AND owner_id = ?",
                (visibility, filename, owner_id)
            )
            if cursor.rowcount == 0:
                raise ValueError("File not found or permission denied")
    finally:
        conn.close()


def add_file_share(filename: str, shared_with_username: str, owner_id: int) -> None:
    """Share a file owned by owner_id with shared_with_username."""
    conn = get_connection()
    try:
        # 1. Fetch file and verify owner
        file_row = conn.execute(
            "SELECT id, owner_id, share_token FROM files WHERE filename = ?",
            (filename,)
        ).fetchone()
        if not file_row:
            raise ValueError(f"File not found: {filename}")
        if file_row["owner_id"] != owner_id:
            raise ValueError(f"Permission denied: user does not own file {filename}")
        
        # 2. Fetch shared_with user
        user_row = conn.execute(
            "SELECT id FROM users WHERE username = ?",
            (shared_with_username,)
        ).fetchone()
        if not user_row:
            raise ValueError(f"User not found: {shared_with_username}")
        
        file_id = file_row["id"]
        shared_with_user_id = user_row["id"]
        
        # 3. Generate share_token if missing
        share_token = file_row["share_token"]
        
        with conn:
            if not share_token:
                share_token = str(uuid.uuid4())
                conn.execute(
                    "UPDATE files SET share_token = ? WHERE id = ?",
                    (share_token, file_id)
                )
            # 4. Insert into file_shares
            conn.execute(
                """
                INSERT OR IGNORE INTO file_shares (file_id, shared_with_user_id)
                VALUES (?, ?)
                """,
                (file_id, shared_with_user_id)
            )
    finally:
        conn.close()


def remove_file_share(filename: str, shared_with_username: str, owner_id: int) -> None:
    """Remove a file share for the given user."""
    conn = get_connection()
    try:
        # 1. Fetch file and verify owner
        file_row = conn.execute(
            "SELECT id, owner_id FROM files WHERE filename = ?",
            (filename,)
        ).fetchone()
        if not file_row:
            raise ValueError(f"File not found: {filename}")
        if file_row["owner_id"] != owner_id:
            raise ValueError(f"Permission denied: user does not own file {filename}")
        
        # 2. Fetch shared_with user
        user_row = conn.execute(
            "SELECT id FROM users WHERE username = ?",
            (shared_with_username,)
        ).fetchone()
        if not user_row:
            raise ValueError(f"User not found: {shared_with_username}")
        
        file_id = file_row["id"]
        shared_with_user_id = user_row["id"]
        
        with conn:
            conn.execute(
                "DELETE FROM file_shares WHERE file_id = ? AND shared_with_user_id = ?",
                (file_id, shared_with_user_id)
            )
    finally:
        conn.close()


def update_file_sharing(filename: str, owner_id: int, visibility: str, usernames_list: list[str]) -> str:
    """Updates visibility, generates share token if needed, and sets explicit shares in a single transaction.
    
    Raises ValueError on validation or write failure, rolling back all changes.
    Returns the share_token (existing or newly generated).
    """
    if visibility not in ('private', 'public', 'shared'):
        raise ValueError("Visibility must be 'private', 'public', or 'shared'")
    
    conn = get_connection()
    try:
        with conn:
            # 1. Fetch file and verify owner
            file_row = conn.execute(
                "SELECT id, owner_id, share_token FROM files WHERE filename = ?",
                (filename,)
            ).fetchone()
            if not file_row:
                raise ValueError("File not found")
            if file_row["owner_id"] != owner_id:
                raise ValueError("Permission denied: user does not own file")
                
            # 2. Update visibility
            conn.execute(
                "UPDATE files SET visibility = ? WHERE id = ?",
                (visibility, file_row["id"])
            )
            
            # 3. Generate share token if one doesn't exist
            share_token = file_row["share_token"]
            if not share_token:
                share_token = str(uuid.uuid4())
                conn.execute(
                    "UPDATE files SET share_token = ? WHERE id = ?",
                    (share_token, file_row["id"])
                )
                
            # 4. Update file shares ONLY when visibility is 'shared'
            if visibility == 'shared':
                # Check if all usernames in usernames_list exist
                for u in usernames_list:
                    user_exists = conn.execute("SELECT id FROM users WHERE username = ?", (u,)).fetchone()
                    if not user_exists:
                        raise ValueError(f"User '{u}' does not exist")

                # Fetch current shares to compute the diff (adds and removes)
                current_shares = conn.execute(
                    """
                    SELECT u.username, fs.shared_with_user_id
                    FROM file_shares fs
                    JOIN users u ON fs.shared_with_user_id = u.id
                    WHERE fs.file_id = ?
                    """,
                    (file_row["id"],)
                ).fetchall()
                
                current_usernames = [row["username"] for row in current_shares]
                
                # Add shares that are in usernames_list but not in current_usernames
                for u in usernames_list:
                    if u not in current_usernames:
                        user_row = conn.execute("SELECT id FROM users WHERE username = ?", (u,)).fetchone()
                        conn.execute(
                            "INSERT OR IGNORE INTO file_shares (file_id, shared_with_user_id) VALUES (?, ?)",
                            (file_row["id"], user_row["id"])
                        )
                
                # Remove shares that are in current_usernames but not in usernames_list
                for row in current_shares:
                    u = row["username"]
                    shared_with_user_id = row["shared_with_user_id"]
                    if u not in usernames_list:
                        conn.execute(
                            "DELETE FROM file_shares WHERE file_id = ? AND shared_with_user_id = ?",
                            (file_row["id"], shared_with_user_id)
                        )
                    
            return share_token
    finally:
        conn.close()


def get_file_by_share_token(token: str) -> dict[str, Any] | None:
    """Return the file row for the given share token, or None if not found."""
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT f.id, f.filename, f.original_name, f.file_type,
                   f.file_size_bytes, f.uploaded_at, f.owner_id,
                   f.folder_id, f.visibility, f.share_token,
                   u.username AS owner_username
            FROM files f
            JOIN users u ON f.owner_id = u.id
            WHERE f.share_token = ?
            """,
            (token,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_file_shares(file_id: int) -> list[str]:
    """Return list of usernames a file is explicitly shared with."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT u.username FROM file_shares fs
            JOIN users u ON fs.shared_with_user_id = u.id
            WHERE fs.file_id = ?
            """,
            (file_id,)
        ).fetchall()
        return [row["username"] for row in rows]
    finally:
        conn.close()



def get_visible_files_for_user(viewer_user_id: int, folder_id: int | None = None,
                               search: str | None = None, file_type: str | None = None) -> list[dict[str, Any]]:
    """Return all files visible to the viewer_user_id, filtered by folder, search, and type."""
    conn = get_connection()
    try:
        query = """
            SELECT f.id, f.filename, f.original_name, f.file_type,
                   f.file_size_bytes, f.uploaded_at, f.owner_id,
                   f.folder_id, f.visibility, f.share_token,
                   u.username AS owner_username
            FROM files f
            JOIN users u ON f.owner_id = u.id
        """
        where_clauses = []
        params = []
        
        # Visibility rule:
        # - owner's own files (any visibility)
        # - OR visibility = 'public'
        # - OR files shared with the viewer (row in file_shares)
        visibility_clause = """
            (
                f.owner_id = ?
                OR f.visibility = 'public'
                OR f.id IN (SELECT file_id FROM file_shares WHERE shared_with_user_id = ?)
            )
        """
        where_clauses.append(visibility_clause)
        params.extend([viewer_user_id, viewer_user_id])
        
        # Folder filter:
        if folder_id is None:
            where_clauses.append("f.folder_id IS NULL")
        else:
            where_clauses.append("f.folder_id = ?")
            params.append(folder_id)
            
        # Search filter:
        if search:
            where_clauses.append("(f.original_name LIKE ? OR f.filename LIKE ?)")
            params.extend([f"%{search}%", f"%{search}%"])
            
        # File type filter:
        if file_type and file_type.lower() != 'all':
            where_clauses.append("LOWER(f.file_type) = ?")
            params.append(file_type.lower())
            
        query += " WHERE " + " AND ".join(where_clauses)
        query += " ORDER BY f.uploaded_at DESC, f.id DESC"
        
        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()
