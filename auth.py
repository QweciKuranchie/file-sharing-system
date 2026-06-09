"""
auth.py — Authentication, JWT management, and quota helpers.

Public API
----------
register_user(username, email, password) → user row dict
login_user(username, password)           → JWT token string
validate_token(token)                    → payload dict  (raises on failure)
decode_token(token)                      → alias for validate_token
get_user_by_id(user_id)                  → user row dict or None
check_quota(user_id, file_size_bytes)    → bool
update_quota(user_id, delta_bytes)       → None
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Any

import jwt
from werkzeug.security import generate_password_hash, check_password_hash

from config import JWT_SECRET_KEY, JWT_ALGORITHM, JWT_EXPIRY_MINUTES
from database import get_connection


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
        # Check for duplicate username
        existing = conn.execute(
            "SELECT id FROM users WHERE username = ?", (username,)
        ).fetchone()
        if existing:
            raise DuplicateUsernameError(f"Username already exists: {username}")

        # Check for duplicate email
        existing = conn.execute(
            "SELECT id FROM users WHERE email = ?", (email,)
        ).fetchone()
        if existing:
            raise DuplicateEmailError(f"Email already registered: {email}")

        # Insert new user
        cursor = conn.execute(
            """
            INSERT INTO users (username, email, password_hash)
            VALUES (?, ?, ?)
            """,
            (username, email, password_hash),
        )
        conn.commit()

        # Return the created user row (without password_hash)
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

def login_user(username: str, password: str) -> str:
    """Authenticate a user and return a signed JWT.

    Parameters
    ----------
    username : str
        The registered username.
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
        If the username does not exist or the password is wrong.
    """
    if not username or not password:
        raise InvalidCredentialsError("Username and password are required")

    conn = get_connection()
    try:
        user = conn.execute(
            "SELECT id, username, password_hash FROM users WHERE username = ?",
            (username.strip(),),
        ).fetchone()

        if user is None:
            raise InvalidCredentialsError("Invalid username or password")

        if not check_password_hash(user["password_hash"], password):
            raise InvalidCredentialsError("Invalid username or password")

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

def validate_token(token: str) -> dict[str, Any]:
    """Decode and verify a JWT.

    Parameters
    ----------
    token : str
        The encoded JWT string (from session or ``Authorization`` header).

    Returns
    -------
    dict
        The decoded payload containing at least ``user_id`` and ``username``.

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


# Alias used by app.py
decode_token = validate_token


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

# Auto-initialize database on module import
init_db()
