"""
test_auth.py — Tests for the BE-1 acceptance criteria.

Run with:  python -m pytest test_auth.py -v
"""

import os
import sys
import time

import pytest

# ---------------------------------------------------------------------------
# Ensure we import the project modules and use a *throwaway* test database
# so we never touch the real fileshare.db.
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(__file__))

# Override DATABASE_PATH BEFORE importing anything that reads config.
import config
_TEST_DB = os.path.join(config.BASE_DIR, "test_fileshare.db")
config.DATABASE_PATH = _TEST_DB
# Use a fixed JWT secret so tokens are reproducible within a test session.
config.JWT_SECRET_KEY = "test-secret-key-do-not-use-in-production"

from database import init_db, get_connection  # noqa: E402
from auth import (                            # noqa: E402
    register_user,
    login_user,
    validate_token,
    check_quota,
    update_quota,
    DuplicateUsernameError,
    DuplicateEmailError,
    InvalidCredentialsError,
    TokenError,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def fresh_database():
    """Create a clean database before every test, remove it afterwards."""
    # Teardown any leftover DB
    for path in [_TEST_DB, _TEST_DB + "-wal", _TEST_DB + "-shm"]:
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    init_db()
    yield

    # Cleanup
    for path in [_TEST_DB, _TEST_DB + "-wal", _TEST_DB + "-shm"]:
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


def _register_default_user():
    """Helper — register a canonical test user."""
    return register_user("kwame", "kwame@example.com", "strongP@ss1")


# ═══════════════════════════════════════════════════════════════════════════
# AC-1  fileshare.db is created automatically with both tables
# ═══════════════════════════════════════════════════════════════════════════

class TestDatabaseInit:
    def test_db_file_created(self):
        assert os.path.exists(_TEST_DB)

    def test_users_table_exists(self):
        conn = get_connection()
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
        ).fetchone()
        conn.close()
        assert tables is not None

    def test_files_table_exists(self):
        conn = get_connection()
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='files'"
        ).fetchone()
        conn.close()
        assert tables is not None

    def test_users_table_columns(self):
        conn = get_connection()
        cols = conn.execute("PRAGMA table_info(users)").fetchall()
        col_names = {c["name"] for c in cols}
        conn.close()
        expected = {
            "id", "username", "email", "password_hash",
            "quota_limit_bytes", "quota_used_bytes", "created_at",
        }
        assert expected.issubset(col_names)

    def test_files_table_columns(self):
        conn = get_connection()
        cols = conn.execute("PRAGMA table_info(files)").fetchall()
        col_names = {c["name"] for c in cols}
        conn.close()
        expected = {
            "id", "filename", "original_name", "file_type",
            "file_size_bytes", "uploaded_at", "owner_id",
        }
        assert expected.issubset(col_names)

    def test_init_db_is_idempotent(self):
        """Calling init_db() twice must not raise or corrupt data."""
        _register_default_user()
        init_db()  # second call
        conn = get_connection()
        count = conn.execute("SELECT COUNT(*) AS cnt FROM users").fetchone()["cnt"]
        conn.close()
        assert count == 1


# ═══════════════════════════════════════════════════════════════════════════
# AC-2  Registration — duplicate username / email raises clear error
# ═══════════════════════════════════════════════════════════════════════════

class TestRegistration:
    def test_register_success(self):
        user = _register_default_user()
        assert user["username"] == "kwame"
        assert user["email"] == "kwame@example.com"
        assert user["id"] is not None
        assert user["quota_limit_bytes"] == 52_428_800
        assert user["quota_used_bytes"] == 0

    def test_duplicate_username_raises(self):
        _register_default_user()
        with pytest.raises(DuplicateUsernameError):
            register_user("kwame", "other@example.com", "pass123")

    def test_duplicate_email_raises(self):
        _register_default_user()
        with pytest.raises(DuplicateEmailError):
            register_user("ama", "kwame@example.com", "pass123")

    def test_empty_username_raises(self):
        with pytest.raises(ValueError):
            register_user("", "a@b.com", "pass")

    def test_empty_email_raises(self):
        with pytest.raises(ValueError):
            register_user("user1", "", "pass")

    def test_empty_password_raises(self):
        with pytest.raises(ValueError):
            register_user("user1", "a@b.com", "")

    def test_email_normalised_to_lowercase(self):
        user = register_user("user1", "Alice@Example.COM", "pass")
        assert user["email"] == "alice@example.com"


# ═══════════════════════════════════════════════════════════════════════════
# AC-3  Passwords stored as hashes — never plain text
# ═══════════════════════════════════════════════════════════════════════════

class TestPasswordStorage:
    def test_password_is_hashed(self):
        _register_default_user()
        conn = get_connection()
        row = conn.execute(
            "SELECT password_hash FROM users WHERE username = 'kwame'"
        ).fetchone()
        conn.close()
        # Must not be the plain text
        assert row["password_hash"] != "strongP@ss1"
        # Werkzeug hashes start with a method identifier
        assert row["password_hash"].startswith(("scrypt:", "pbkdf2:"))


# ═══════════════════════════════════════════════════════════════════════════
# AC-4  Login — correct → JWT; wrong → error
# ═══════════════════════════════════════════════════════════════════════════

class TestLogin:
    def test_login_success_returns_jwt(self):
        _register_default_user()
        token = login_user("kwame", "strongP@ss1")
        assert isinstance(token, str)
        assert len(token) > 0

    def test_login_wrong_password(self):
        _register_default_user()
        with pytest.raises(InvalidCredentialsError):
            login_user("kwame", "wrongPassword")

    def test_login_nonexistent_user(self):
        with pytest.raises(InvalidCredentialsError):
            login_user("nobody", "password")

    def test_login_empty_fields(self):
        with pytest.raises(InvalidCredentialsError):
            login_user("", "")


# ═══════════════════════════════════════════════════════════════════════════
# AC-5  JWT validation rejects expired / tampered tokens
# ═══════════════════════════════════════════════════════════════════════════

class TestJWT:
    def test_valid_token_decodes(self):
        _register_default_user()
        token = login_user("kwame", "strongP@ss1")
        payload = validate_token(token)
        assert payload["user_id"] == 1
        assert payload["username"] == "kwame"

    def test_tampered_token_raises(self):
        _register_default_user()
        token = login_user("kwame", "strongP@ss1")
        tampered = token[:-4] + "XXXX"
        with pytest.raises(TokenError):
            validate_token(tampered)

    def test_expired_token_raises(self):
        """Forge a token with an already-passed expiry to test rejection."""
        import jwt as _jwt
        from datetime import datetime, timezone, timedelta

        payload = {
            "user_id": 1,
            "username": "kwame",
            "exp": datetime.now(timezone.utc) - timedelta(minutes=5),
            "iat": datetime.now(timezone.utc) - timedelta(minutes=31),
        }
        expired_token = _jwt.encode(
            payload, config.JWT_SECRET_KEY, algorithm=config.JWT_ALGORITHM
        )
        with pytest.raises(TokenError, match="expired"):
            validate_token(expired_token)

    def test_garbage_token_raises(self):
        with pytest.raises(TokenError):
            validate_token("not.a.real.token")


# ═══════════════════════════════════════════════════════════════════════════
# AC-6  check_quota returns False when quota_used + file_size > limit
# ═══════════════════════════════════════════════════════════════════════════

class TestCheckQuota:
    def test_within_quota(self):
        user = _register_default_user()
        # User has 50 MB free → 1 MB upload should pass
        assert check_quota(user["id"], 1_048_576) is True

    def test_exactly_at_limit(self):
        user = _register_default_user()
        # Uploading exactly 50 MB when 0 used → should pass (<=)
        assert check_quota(user["id"], 52_428_800) is True

    def test_exceeds_quota(self):
        user = _register_default_user()
        # 50 MB + 1 byte → should fail
        assert check_quota(user["id"], 52_428_801) is False

    def test_exceeds_after_partial_usage(self):
        user = _register_default_user()
        # Use 40 MB, then try to add 20 MB
        update_quota(user["id"], 40_000_000)
        assert check_quota(user["id"], 20_000_000) is False

    def test_nonexistent_user(self):
        assert check_quota(9999, 100) is False


# ═══════════════════════════════════════════════════════════════════════════
# AC-7  update_quota correctly increments / decrements quota_used_bytes
# ═══════════════════════════════════════════════════════════════════════════

class TestUpdateQuota:
    def test_increment(self):
        user = _register_default_user()
        update_quota(user["id"], 5_000_000)
        conn = get_connection()
        row = conn.execute(
            "SELECT quota_used_bytes FROM users WHERE id = ?", (user["id"],)
        ).fetchone()
        conn.close()
        assert row["quota_used_bytes"] == 5_000_000

    def test_decrement(self):
        user = _register_default_user()
        update_quota(user["id"], 5_000_000)
        update_quota(user["id"], -3_000_000)
        conn = get_connection()
        row = conn.execute(
            "SELECT quota_used_bytes FROM users WHERE id = ?", (user["id"],)
        ).fetchone()
        conn.close()
        assert row["quota_used_bytes"] == 2_000_000

    def test_decrement_below_zero_raises(self):
        user = _register_default_user()
        with pytest.raises(ValueError, match="negative"):
            update_quota(user["id"], -1)

    def test_nonexistent_user_raises(self):
        with pytest.raises(ValueError, match="No user found"):
            update_quota(9999, 100)

    def test_multiple_increments(self):
        user = _register_default_user()
        update_quota(user["id"], 1_000_000)
        update_quota(user["id"], 2_000_000)
        update_quota(user["id"], 3_000_000)
        conn = get_connection()
        row = conn.execute(
            "SELECT quota_used_bytes FROM users WHERE id = ?", (user["id"],)
        ).fetchone()
        conn.close()
        assert row["quota_used_bytes"] == 6_000_000
