"""
auth.py — Authentication, JWT management, and quota helpers.

Public API
----------
register_user(username, email, password)  → user row dict
login_user(identifier, password)          → JWT token string  (username or email)
validate_token(token)                     → user_id int  (raises on failure)
decode_token(token)                       → payload dict  (raises on failure)
refresh_token(token)                      → JWT token string  (raises on failure)
get_user_by_id(user_id)                   → user row dict or None
check_quota(user_id, file_size_bytes)     → bool
reserve_quota(user_id, file_size_bytes)   → bool  (atomic check-and-increment)
release_quota(user_id, file_size_bytes)   → None  (atomic rollback decrement)
update_quota(user_id, delta_bytes)        → None

# File metadata (re-exported from database for backwards compatibility)
add_file, get_file_by_name, delete_file, rename_file, get_all_files
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Any

import jwt
from werkzeug.security import generate_password_hash, check_password_hash

from config import JWT_SECRET_KEY, JWT_ALGORITHM, JWT_EXPIRY_MINUTES
from database import (
    get_connection,
    init_db,
    add_file,
    get_file_by_name,
    delete_file,
    rename_file,
    get_all_files,
    delete_file_and_decrement_quota,
)


# ── Custom exceptions ─────────────────────────────────────────────────────

class AuthError(ValueError):
    """Base class for authentication / authorisation errors.

    Inherits from ``ValueError`` so that Flask routes using
    ``except ValueError`` will catch auth-specific exceptions.
    """


class DuplicateUsernameError(AuthError):
    """Raised when a username is already taken."""


class DuplicateEmailError(AuthError):
    """Raised when an email address is already registered."""


class InvalidCredentialsError(AuthError):
    """Raised when login credentials are incorrect."""


class TokenError(AuthError):
    """Raised when a JWT is expired, tampered with, or otherwise invalid."""


class QuotaExceededError(AuthError):
    """Raised when an upload would push the user over their storage quota."""


# ── Registration ──────────────────────────────────────────────────────────

def register_user(username: str, email: str, password: str) -> dict[str, Any]:
    """Create a new user account.

    Parameters
    ----------
    username : str
        Desired username (must be unique, case-sensitive).
    email : str
        Email address (must be unique, case-insensitive check).
    password : str
        Plain-text password — will be hashed before storage.

    Returns
    -------
    dict
        The newly created user row (``id``, ``username``, ``email``,
        ``quota_limit_bytes``, ``quota_used_bytes``, ``created_at``).

    Raises
    ------
    DuplicateUsernameError
        If ``username`` is already taken.
    DuplicateEmailError
        If ``email`` is already registered.
    ValueError
        If any of the three fields is empty / whitespace-only.
    """
    # ── Input validation ──────────────────────────────────────────────
    if not username or not username.strip():
        raise ValueError("Username must not be empty")
    if not email or not email.strip():
        raise ValueError("Email must not be empty")
    if not password:
        raise ValueError("Password must not be empty")

    username = username.strip()
    email = email.strip().lower()
    password_hash = generate_password_hash(password)

    conn = get_connection()
    try:
        # Optimistic pre-checks for a friendly UX error message, but the
        # INSERT below is the *authoritative* uniqueness check.  A concurrent
        # insert can still slip through the SELECT window, so we also catch
        # sqlite3.IntegrityError from the INSERT itself.
        existing = conn.execute(
            "SELECT id FROM users WHERE username = ?", (username,)
        ).fetchone()
        if existing:
            raise DuplicateUsernameError(f"Username already exists: {username}")

        existing = conn.execute(
            "SELECT id FROM users WHERE email = ?", (email,)
        ).fetchone()
        if existing:
            raise DuplicateEmailError(f"Email already registered: {email}")

        # INSERT is the authoritative uniqueness check.
        created_at = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        try:
            cursor = conn.execute(
                """
                INSERT INTO users (username, email, password_hash, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (username, email, password_hash, created_at),
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            # Translate the constraint name to a typed exception.
            msg = str(exc).lower()
            if "username" in msg:
                raise DuplicateUsernameError(
                    f"Username already exists: {username}"
                ) from exc
            if "email" in msg:
                raise DuplicateEmailError(
                    f"Email already registered: {email}"
                ) from exc
            # Unknown constraint — re-raise as generic auth error.
            raise AuthError(f"Registration failed: {exc}") from exc

        # Return the created user row (without password_hash).
        user = conn.execute(
            """
            SELECT id, username, email, quota_limit_bytes,
                   quota_used_bytes, created_at
            FROM users WHERE id = ?
            """,
            (cursor.lastrowid,),
        ).fetchone()

        return dict(user)
    finally:
        conn.close()


# ── Login ─────────────────────────────────────────────────────────────────

def login_user(identifier: str, password: str) -> str:
    """Authenticate a user and return a signed JWT.

    Parameters
    ----------
    identifier : str
        The registered username or email address.  If the value contains
        ``@`` it is treated as an email (normalized to lowercase); otherwise
        it is matched as a username.
    password : str
        The plain-text password to verify.

    Returns
    -------
    str
        A signed JWT containing ``user_id``, ``username``, and ``exp``
        (30-minute expiry from now).

    Raises
    ------
    InvalidCredentialsError
        If the identifier does not match any user or the password is wrong.
    """
    if not identifier or not password:
        raise InvalidCredentialsError("Username and password are required")

    identifier = identifier.strip()

    conn = get_connection()
    try:
        if "@" in identifier:
            # Treat as email — normalise to lowercase for case-insensitive match
            user = conn.execute(
                "SELECT id, username, password_hash FROM users WHERE email = ?",
                (identifier.lower(),),
            ).fetchone()
        else:
            user = conn.execute(
                "SELECT id, username, password_hash FROM users WHERE username = ?",
                (identifier,),
            ).fetchone()

        if user is None:
            raise InvalidCredentialsError("Invalid username/email or password")

        if not check_password_hash(user["password_hash"], password):
            raise InvalidCredentialsError("Invalid username/email or password")

        # Build JWT payload
        now = datetime.now(timezone.utc)
        payload = {
            "user_id": user["id"],
            "username": user["username"],
            "exp": now + timedelta(minutes=JWT_EXPIRY_MINUTES),
            "iat": now,
        }

        token = jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)
        return token
    finally:
        conn.close()


# ── JWT Validation ────────────────────────────────────────────────────────

def validate_token(token: str) -> int:
    """Decode and verify a JWT, returning only the user identifier.

    Parameters
    ----------
    token : str
        The encoded JWT string (from session or ``Authorization`` header).

    Returns
    -------
    int
        The ``user_id`` extracted from the validated payload.

    Raises
    ------
    TokenError
        If the token is expired, tampered with, or otherwise invalid.
    """
    try:
        payload = jwt.decode(
            token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM]
        )
        return payload["user_id"]
    except jwt.ExpiredSignatureError:
        raise TokenError("Token has expired — please log in again")
    except jwt.InvalidTokenError as exc:
        raise TokenError(f"Invalid token: {exc}")


def decode_token(token: str) -> dict[str, Any]:
    """Decode and verify a JWT, returning the full payload.

    Use this when callers need the complete payload (``user_id``,
    ``username``, ``exp``, ``iat``).  For callers that only need the
    user identifier, prefer :func:`validate_token`.

    Parameters
    ----------
    token : str
        The encoded JWT string.

    Returns
    -------
    dict
        The decoded payload containing ``user_id``, ``username``,
        ``exp``, and ``iat``.

    Raises
    ------
    TokenError
        If the token is expired, tampered with, or otherwise invalid.
    """
    try:
        payload = jwt.decode(
            token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM]
        )
        return payload
    except jwt.ExpiredSignatureError:
        raise TokenError("Token has expired — please log in again")
    except jwt.InvalidTokenError as exc:
        raise TokenError(f"Invalid token: {exc}")


def refresh_token(token: str) -> str:
    """Decode token and return a new token with extended expiry if valid.

    Parameters
    ----------
    token : str
        The current encoded JWT token.

    Returns
    -------
    str
        A new signed JWT token with updated expiry (30 minutes from now).

    Raises
    ------
    TokenError
        If the token is invalid or expired.
    """
    payload = decode_token(token)
    now = datetime.now(timezone.utc)
    payload["exp"] = now + timedelta(minutes=JWT_EXPIRY_MINUTES)
    payload["iat"] = now
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


# ── User lookup ───────────────────────────────────────────────────────────

def get_user_by_id(user_id: int) -> dict[str, Any] | None:
    """Fetch a user row by primary key.

    Parameters
    ----------
    user_id : int
        The user's ``id`` column value.

    Returns
    -------
    dict or None
        A dict with ``id``, ``username``, ``email``, ``quota_limit_bytes``,
        ``quota_used_bytes``, and ``created_at``.  Returns ``None`` if the
        user does not exist.
    """
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT id, username, email, quota_limit_bytes,
                   quota_used_bytes, created_at
            FROM users WHERE id = ?
            """,
            (user_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ── Quota helpers ─────────────────────────────────────────────────────────

def check_quota(user_id: int, file_size_bytes: int) -> bool:
    """Return ``True`` if the user has enough remaining quota for the upload.

    Parameters
    ----------
    user_id : int
        The primary-key id of the user.
    file_size_bytes : int
        Size of the file about to be uploaded (in bytes).

    Returns
    -------
    bool
        ``True`` if ``quota_used_bytes + file_size_bytes <= quota_limit_bytes``,
        ``False`` otherwise.
    """
    conn = get_connection()
    try:
        user = conn.execute(
            "SELECT quota_limit_bytes, quota_used_bytes FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()

        if user is None:
            return False

        return (user["quota_used_bytes"] + file_size_bytes) <= user["quota_limit_bytes"]
    finally:
        conn.close()


def update_quota(user_id: int, delta_bytes: int) -> None:
    """Adjust the user's ``quota_used_bytes`` by *delta_bytes*.

    Parameters
    ----------
    user_id : int
        The primary-key id of the user.
    delta_bytes : int
        Positive value to **increase** usage (after upload), or negative
        value to **decrease** usage (after delete).

    Raises
    ------
    ValueError
        If the update would cause ``quota_used_bytes`` to drop below zero.
    """
    conn = get_connection()
    try:
        user = conn.execute(
            "SELECT quota_used_bytes FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()

        if user is None:
            raise ValueError(f"No user found with id {user_id}")

        new_usage = user["quota_used_bytes"] + delta_bytes
        if new_usage < 0:
            raise ValueError(
                f"Quota update would result in negative usage "
                f"(current={user['quota_used_bytes']}, delta={delta_bytes})"
            )

        conn.execute(
            "UPDATE users SET quota_used_bytes = ? WHERE id = ?",
            (new_usage, user_id),
        )
        conn.commit()
    finally:
        conn.close()


def reserve_quota(user_id: int, file_size_bytes: int) -> bool:
    """Atomically check and reserve quota for an upload in a single statement.

    Uses a conditional ``UPDATE … WHERE quota_used_bytes + ? <= quota_limit_bytes``
    so the check and increment happen inside one database write with no
    time-of-check / time-of-use window between them.

    Parameters
    ----------
    user_id : int
        The primary-key id of the user.
    file_size_bytes : int
        The number of bytes to reserve.

    Returns
    -------
    bool
        ``True`` if quota was successfully reserved, ``False`` if the user
        does not exist or the upload would exceed their limit.
    """
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            UPDATE users
               SET quota_used_bytes = quota_used_bytes + ?
             WHERE id = ?
               AND quota_used_bytes + ? <= quota_limit_bytes
            """,
            (file_size_bytes, user_id, file_size_bytes),
        )
        conn.commit()
        # rowcount == 1 means the condition was satisfied and the row updated.
        return cursor.rowcount == 1
    finally:
        conn.close()


def release_quota(user_id: int, file_size_bytes: int) -> None:
    """Atomically roll back a previously reserved quota amount.

    Called by the upload route's failure-compensation path after a successful
    ``reserve_quota`` but a downstream failure (TCP save or DB metadata write).
    Uses a guarded ``UPDATE`` so ``quota_used_bytes`` can never go below zero.

    Parameters
    ----------
    user_id : int
        The primary-key id of the user.
    file_size_bytes : int
        The number of bytes to release (must be positive).

    Raises
    ------
    ValueError
        If the user does not exist or the decrement would underflow zero
        (indicates a programming error — reserve/release mismatch).
    """
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            UPDATE users
               SET quota_used_bytes = quota_used_bytes - ?
             WHERE id = ?
               AND quota_used_bytes - ? >= 0
            """,
            (file_size_bytes, user_id, file_size_bytes),
        )
        conn.commit()
        if cursor.rowcount == 0:
            # Either user not found or underflow guard triggered.
            user_exists = conn.execute(
                "SELECT 1 FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if not user_exists:
                raise ValueError(f"No user found with id {user_id}")
            raise ValueError(
                f"release_quota underflow: releasing {file_size_bytes} bytes "
                f"would drop quota_used_bytes below zero for user {user_id}"
            )
    finally:
        conn.close()


# Auto-initialize database on module import
init_db()
