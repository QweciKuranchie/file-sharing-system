"""
test_auth.py — Tests for the BE-1 acceptance criteria.

Run with:  python -m pytest test_auth.py -v
"""

import os
import sys
import time
from unittest.mock import patch

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
    reserve_quota,
    release_quota,
    update_quota,
    DuplicateUsernameError,
    DuplicateEmailError,
    InvalidCredentialsError,
    TokenError,
    AuthError,
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

    def test_login_with_email_success(self):
        _register_default_user()
        token = login_user("kwame@example.com", "strongP@ss1")
        assert isinstance(token, str)
        assert len(token) > 0
        # Verify the token contains the right user
        from auth import decode_token
        payload = decode_token(token)
        assert payload["username"] == "kwame"

    def test_login_with_email_case_insensitive(self):
        _register_default_user()
        token = login_user("Kwame@EXAMPLE.com", "strongP@ss1")
        assert isinstance(token, str)
        assert len(token) > 0

    def test_login_with_nonexistent_email(self):
        _register_default_user()
        with pytest.raises(InvalidCredentialsError):
            login_user("nobody@example.com", "strongP@ss1")


# ═══════════════════════════════════════════════════════════════════════════
# AC-5  JWT validation rejects expired / tampered tokens
# ═══════════════════════════════════════════════════════════════════════════

class TestJWT:
    def test_valid_token_decodes(self):
        _register_default_user()
        token = login_user("kwame", "strongP@ss1")

        # validate_token returns just the user_id (int)
        user_id = validate_token(token)
        assert isinstance(user_id, int)
        assert user_id == 1

        # decode_token returns the full payload (dict)
        from auth import decode_token
        payload = decode_token(token)
        assert isinstance(payload, dict)
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


# ═══════════════════════════════════════════════════════════════════════════
# AC-8  reserve_quota — atomic check-and-increment
# ═══════════════════════════════════════════════════════════════════════════

class TestReserveQuota:
    def test_reserve_success(self):
        """reserve_quota returns True and increments quota_used_bytes."""
        user = _register_default_user()
        result = reserve_quota(user["id"], 1_000_000)
        assert result is True
        conn = get_connection()
        row = conn.execute(
            "SELECT quota_used_bytes FROM users WHERE id = ?", (user["id"],)
        ).fetchone()
        conn.close()
        assert row["quota_used_bytes"] == 1_000_000

    def test_reserve_exactly_at_limit(self):
        """Reserving the full quota limit in one shot should succeed."""
        user = _register_default_user()
        result = reserve_quota(user["id"], 52_428_800)  # exactly 50 MB
        assert result is True
        conn = get_connection()
        row = conn.execute(
            "SELECT quota_used_bytes FROM users WHERE id = ?", (user["id"],)
        ).fetchone()
        conn.close()
        assert row["quota_used_bytes"] == 52_428_800

    def test_reserve_exceeds_quota(self):
        """reserve_quota returns False and does NOT change quota_used_bytes."""
        user = _register_default_user()
        result = reserve_quota(user["id"], 52_428_801)  # 1 byte over
        assert result is False
        conn = get_connection()
        row = conn.execute(
            "SELECT quota_used_bytes FROM users WHERE id = ?", (user["id"],)
        ).fetchone()
        conn.close()
        assert row["quota_used_bytes"] == 0  # unchanged

    def test_reserve_after_partial_usage_exceeds(self):
        """reserve_quota fails when remaining space is insufficient."""
        user = _register_default_user()
        update_quota(user["id"], 40_000_000)   # pre-fill 40 MB
        result = reserve_quota(user["id"], 20_000_000)  # needs 20 MB, only 12 left
        assert result is False
        conn = get_connection()
        row = conn.execute(
            "SELECT quota_used_bytes FROM users WHERE id = ?", (user["id"],)
        ).fetchone()
        conn.close()
        assert row["quota_used_bytes"] == 40_000_000  # still 40 MB, not changed

    def test_reserve_nonexistent_user(self):
        """reserve_quota returns False for a user that doesn't exist."""
        result = reserve_quota(9999, 100)
        assert result is False

    def test_reserve_rollback_on_downstream_failure(self):
        """
        Simulate: reserve succeeds, then downstream fails, then release_quota
        restores the original quota_used_bytes.
        """
        user = _register_default_user()
        file_size = 5_000_000

        reserved = reserve_quota(user["id"], file_size)
        assert reserved is True

        # Simulate downstream (TCP / DB metadata) failure — call release_quota
        release_quota(user["id"], file_size)

        conn = get_connection()
        row = conn.execute(
            "SELECT quota_used_bytes FROM users WHERE id = ?", (user["id"],)
        ).fetchone()
        conn.close()
        assert row["quota_used_bytes"] == 0  # fully rolled back

    def test_concurrent_reserve_only_one_succeeds_when_near_limit(self):
        """
        Simulate two concurrent upload requests against the same user who has
        only enough quota for one of them.

        Because reserve_quota is a single conditional UPDATE, SQLite's
        serialised write model guarantees exactly one of the two concurrent
        calls wins the check-and-increment race.
        """
        import threading

        user = _register_default_user()
        # Leave only 8 MB of quota available.
        limit = 52_428_800
        used = limit - 8_000_000
        update_quota(user["id"], used)

        file_size = 5_000_000   # each request wants 5 MB (8 MB only fits one)
        results: list[bool] = []
        lock = threading.Lock()

        def do_reserve():
            ok = reserve_quota(user["id"], file_size)
            with lock:
                results.append(ok)

        t1 = threading.Thread(target=do_reserve)
        t2 = threading.Thread(target=do_reserve)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Exactly one should have succeeded.
        assert results.count(True) == 1
        assert results.count(False) == 1

        # DB state: only 5 MB added on top of the pre-filled amount.
        conn = get_connection()
        row = conn.execute(
            "SELECT quota_used_bytes FROM users WHERE id = ?", (user["id"],)
        ).fetchone()
        conn.close()
        assert row["quota_used_bytes"] == used + file_size


# ═══════════════════════════════════════════════════════════════════════════
# AC-9  release_quota — atomic rollback decrement
# ═══════════════════════════════════════════════════════════════════════════

class TestReleaseQuota:
    def test_release_decrements_correctly(self):
        """release_quota subtracts the reserved amount from quota_used_bytes."""
        user = _register_default_user()
        reserve_quota(user["id"], 10_000_000)
        release_quota(user["id"], 10_000_000)
        conn = get_connection()
        row = conn.execute(
            "SELECT quota_used_bytes FROM users WHERE id = ?", (user["id"],)
        ).fetchone()
        conn.close()
        assert row["quota_used_bytes"] == 0

    def test_release_underflow_raises(self):
        """release_quota raises ValueError if it would push quota below zero."""
        user = _register_default_user()  # quota_used_bytes starts at 0
        with pytest.raises(ValueError, match="below zero"):
            release_quota(user["id"], 1)  # can't release what was never reserved

    def test_release_nonexistent_user_raises(self):
        with pytest.raises(ValueError, match="No user found"):
            release_quota(9999, 100)


# ═══════════════════════════════════════════════════════════════════════════
# AC-10  register_user — IntegrityError regression (concurrency bypass)
# ═══════════════════════════════════════════════════════════════════════════

class TestRegisterUserIntegrityError:
    """
    Regression: if two signup requests race past the SELECT checks at the
    same time, the loser hits sqlite3.IntegrityError on INSERT.  Verify that
    register_user translates that into the module's public exception types
    rather than letting raw SQLite errors escape.
    """

    def test_duplicate_username_via_direct_insert_raises_typed_error(self):
        """Bypass the pre-check SELECTs to simulate the concurrent-insert race."""
        _register_default_user()  # kwame is now in the DB

        # Manually insert a conflicting row to simulate the concurrent winner.
        # Then call register_user with the same username — the SELECT pre-check
        # will fire, but we also want to verify the IntegrityError path fires
        # when the SELECT is skipped entirely.
        import sqlite3 as _sqlite3
        conn = get_connection()
        # Bypass register_user’s SELECT checks by inserting directly.
        # This puts a second row with the same username into the DB.
        # Then we assert that register_user still raises DuplicateUsernameError
        # (i.e., the IntegrityError handler works).
        #
        # We patch the SELECT to return nothing so the pre-check passes, and
        # only the INSERT itself triggers IntegrityError.
        from unittest.mock import patch, MagicMock

        fake_none = MagicMock()
        fake_none.fetchone.return_value = None  # fool the pre-check SELECTs

        original_execute = conn.__class__.execute
        call_count = [0]

        def patched_execute(self_conn, sql, params=()):
            call_count[0] += 1
            # Let the INSERT go through to the real DB — which will
            # raise IntegrityError because 'kwame' already exists.
            return original_execute(self_conn, sql, params)

        with patch("auth.get_connection") as mock_conn_factory:
            real_conn = get_connection()
            mock_conn_factory.return_value = real_conn

            # Patch execute on the real connection to skip the SELECT checks.
            original_real_execute = real_conn.execute
            select_calls = [0]

            def selective_execute(sql, params=()):
                stripped = sql.strip().upper()
                if stripped.startswith("SELECT") and "FROM USERS" in stripped:
                    select_calls[0] += 1
                    if select_calls[0] <= 2:  # skip the two pre-check SELECTs
                        return fake_none
                return original_real_execute(sql, params)

            real_conn.execute = selective_execute  # type: ignore[method-assign]

            with pytest.raises(DuplicateUsernameError):
                register_user("kwame", "other@example.com", "pass123")

    def test_duplicate_email_via_direct_insert_raises_typed_error(self):
        """Same race condition for email uniqueness."""
        _register_default_user()  # kwame@example.com is now in the DB

        from unittest.mock import patch, MagicMock

        fake_none = MagicMock()
        fake_none.fetchone.return_value = None

        with patch("auth.get_connection") as mock_conn_factory:
            real_conn = get_connection()
            mock_conn_factory.return_value = real_conn

            original_real_execute = real_conn.execute
            select_calls = [0]

            def selective_execute(sql, params=()):
                stripped = sql.strip().upper()
                if stripped.startswith("SELECT") and "FROM USERS" in stripped:
                    select_calls[0] += 1
                    if select_calls[0] <= 2:
                        return fake_none
                return original_real_execute(sql, params)

            real_conn.execute = selective_execute  # type: ignore[method-assign]

            with pytest.raises((DuplicateEmailError, DuplicateUsernameError, AuthError)):
                register_user("newuser", "kwame@example.com", "pass123")


# ═══════════════════════════════════════════════════════════════════════════
# Timestamp Standardization and Migration tests
# ═══════════════════════════════════════════════════════════════════════════

import re
from database import normalize_timestamp, normalize_existing_timestamps, add_file

class TestTimestampStandardization:
    def test_timestamp_format_on_registration(self):
        user = register_user("newuser", "newuser@example.com", "password123")
        assert "created_at" in user
        # Matches format YYYY-MM-DDTHH:MM:SSZ
        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", user["created_at"]) is not None

    def test_timestamp_format_on_add_file(self):
        user = register_user("newuser2", "newuser2@example.com", "password123")
        add_file("test_file.txt", "test_file.txt", "text/plain", 100, user["id"])
        
        conn = get_connection()
        row = conn.execute("SELECT uploaded_at FROM files WHERE filename = 'test_file.txt'").fetchone()
        conn.close()
        
        assert row is not None
        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", row["uploaded_at"]) is not None

    def test_normalize_timestamp_helper(self):
        # space-separated SQLite format
        assert normalize_timestamp("2026-06-09 11:20:30") == "2026-06-09T11:20:30Z"
        # ISO format with +00:00 and microseconds
        assert normalize_timestamp("2026-06-09T11:20:30.123456+00:00") == "2026-06-09T11:20:30Z"
        # Standard format should remain untouched
        assert normalize_timestamp("2026-06-09T11:20:30Z") == "2026-06-09T11:20:30Z"
        # ISO format with +02:00 (timezone offset conversion)
        assert normalize_timestamp("2026-06-09T13:20:30+02:00") == "2026-06-09T11:20:30Z"

    def test_normalize_existing_timestamps_migration(self):
        # We manually insert rows with different timestamp formats bypassing validators
        conn = get_connection()
        
        # Clear out tables
        with conn:
            conn.execute("DELETE FROM files")
            conn.execute("DELETE FROM users")
            
            # Insert direct user rows with raw SQL
            conn.execute(
                """
                INSERT INTO users (id, username, email, password_hash, created_at)
                VALUES (1, 'userA', 'usera@example.com', 'hash', '2026-06-09 11:20:30')
                """
            )
            conn.execute(
                """
                INSERT INTO users (id, username, email, password_hash, created_at)
                VALUES (2, 'userB', 'userb@example.com', 'hash', '2026-06-09T11:20:30.123456+00:00')
                """
            )
            conn.execute(
                """
                INSERT INTO users (id, username, email, password_hash, created_at)
                VALUES (3, 'userC', 'userc@example.com', 'hash', '2026-06-09T11:20:30Z')
                """
            )
            
            # Insert direct file rows with raw SQL
            conn.execute(
                """
                INSERT INTO files (id, filename, original_name, file_type, file_size_bytes, uploaded_at, owner_id)
                VALUES (1, 'file1.txt', 'file1.txt', 'text/plain', 10, '2026-06-09 11:20:30', 1)
                """
            )
            conn.execute(
                """
                INSERT INTO files (id, filename, original_name, file_type, file_size_bytes, uploaded_at, owner_id)
                VALUES (2, 'file2.txt', 'file2.txt', 'text/plain', 20, '2026-06-09T11:20:30.123456+00:00', 2)
                """
            )
        conn.close()
        
        # Run normalization function
        normalize_existing_timestamps()
        
        # Verify
        conn = get_connection()
        users = conn.execute("SELECT id, created_at FROM users ORDER BY id").fetchall()
        for u in users:
            assert u["created_at"] == "2026-06-09T11:20:30Z"
            
        files = conn.execute("SELECT id, uploaded_at FROM files ORDER BY id").fetchall()
        for f in files:
            assert f["uploaded_at"] == "2026-06-09T11:20:30Z"
            
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════
# Primary Server Validation tests
# ═══════════════════════════════════════════════════════════════════════════

from primary_server import handle_client
from config import SHARED_FILES_DIR

class MockSocket:
    def __init__(self, command: bytes):
        self.sent_data = bytearray()
        self.recv_data = command
        self.closed = False

    def recv(self, bufsize):
        if not self.recv_data:
            return b""
        chunk = self.recv_data[:bufsize]
        self.recv_data = self.recv_data[bufsize:]
        return chunk

    def sendall(self, data):
        self.sent_data.extend(data)

    def close(self):
        self.closed = True

    def settimeout(self, timeout):
        pass

class TestPrimaryServer:
    def test_negative_upload_size(self):
        # Clean up any potential files
        target_file = os.path.join(SHARED_FILES_DIR, "negative_test.txt")
        if os.path.exists(target_file):
            try:
                os.remove(target_file)
            except OSError:
                pass
            
        from config import TCP_SHARED_SECRET
        cmd = f"AUTH {TCP_SHARED_SECRET}\nUPLOAD negative_test.txt -50\n".encode('utf-8')
        sock = MockSocket(cmd)
        handle_client(sock, ("127.0.0.1", 12345))
        
        # Verify response contains error
        assert b"ERROR" in sock.sent_data
        # Verify no file is created
        assert not os.path.exists(target_file)

    @patch('replication.propagate_delete')
    def test_delete_collision_safety(self, mock_prop):
        from replication import ReplicationOutcome
        from config import TCP_SHARED_SECRET
        mock_prop.return_value = ReplicationOutcome.SUCCESS

        # Ensure directory exists
        os.makedirs(SHARED_FILES_DIR, exist_ok=True)
        
        target_file = os.path.join(SHARED_FILES_DIR, "collision_test.txt")
        temp_del_file = os.path.join(SHARED_FILES_DIR, "collision_test.txt.tmp_del")
        
        with open(target_file, "w") as f:
            f.write("original file")
        with open(temp_del_file, "w") as f:
            f.write("do not delete me")

        # Now send delete command
        cmd = f"AUTH {TCP_SHARED_SECRET}\nDELETE collision_test.txt\n".encode('utf-8')
        sock = MockSocket(cmd)
        handle_client(sock, ("127.0.0.1", 12345))

        # Original target file should be deleted
        assert not os.path.exists(target_file)
        # The .tmp_del file must NOT be deleted!
        assert os.path.exists(temp_del_file)
        assert b"OK FILE_DELETED" in sock.sent_data

        # Clean up
        if os.path.exists(temp_del_file):
            os.remove(temp_del_file)

    @patch('replication.propagate_delete')
    def test_delete_rejection_rollback(self, mock_prop):
        from replication import ReplicationOutcome
        from config import TCP_SHARED_SECRET
        mock_prop.return_value = ReplicationOutcome.REJECTED

        os.makedirs(SHARED_FILES_DIR, exist_ok=True)
        target_file = os.path.join(SHARED_FILES_DIR, "rollback_test.txt")
        with open(target_file, "w") as f:
            f.write("rollback test")

        cmd = f"AUTH {TCP_SHARED_SECRET}\nDELETE rollback_test.txt\n".encode('utf-8')
        sock = MockSocket(cmd)
        handle_client(sock, ("127.0.0.1", 12345))

        # Deleted on primary initially, but since rejected, rolled back!
        assert os.path.exists(target_file)
        assert b"ERROR REPLICATION_FAILED" in sock.sent_data

        # Clean up
        if os.path.exists(target_file):
            os.remove(target_file)

    @patch('replication.propagate_delete')
    def test_delete_ambiguous_rollback(self, mock_prop):
        from replication import ReplicationOutcome
        from config import TCP_SHARED_SECRET
        mock_prop.return_value = ReplicationOutcome.AMBIGUOUS

        os.makedirs(SHARED_FILES_DIR, exist_ok=True)
        target_file = os.path.join(SHARED_FILES_DIR, "ambig_test.txt")
        with open(target_file, "w") as f:
            f.write("ambig test")

        cmd = f"AUTH {TCP_SHARED_SECRET}\nDELETE ambig_test.txt\n".encode('utf-8')
        sock = MockSocket(cmd)
        handle_client(sock, ("127.0.0.1", 12345))

        # Ambiguous outcome -> rolled back on primary!
        assert os.path.exists(target_file)
        assert b"ERROR REPLICATION_FAILED" in sock.sent_data

        # Clean up
        if os.path.exists(target_file):
            os.remove(target_file)

    @patch('replication.propagate_rename')
    def test_rename_rejection_rollback(self, mock_prop):
        from replication import ReplicationOutcome
        from config import TCP_SHARED_SECRET
        mock_prop.return_value = ReplicationOutcome.REJECTED

        os.makedirs(SHARED_FILES_DIR, exist_ok=True)
        old_path = os.path.join(SHARED_FILES_DIR, "rename_old.txt")
        new_path = os.path.join(SHARED_FILES_DIR, "rename_new.txt")
        with open(old_path, "w") as f:
            f.write("rename test")
        if os.path.exists(new_path):
            os.remove(new_path)

        cmd = f"AUTH {TCP_SHARED_SECRET}\nRENAME rename_old.txt rename_new.txt\n".encode('utf-8')
        sock = MockSocket(cmd)
        handle_client(sock, ("127.0.0.1", 12345))

        # Rejected -> rolled back to rename_old.txt!
        assert os.path.exists(old_path)
        assert not os.path.exists(new_path)
        assert b"ERROR REPLICATION_FAILED" in sock.sent_data

        # Clean up
        if os.path.exists(old_path):
            os.remove(old_path)

    @patch('replication.propagate_rename')
    def test_rename_ambiguous_rollback(self, mock_prop):
        from replication import ReplicationOutcome
        from config import TCP_SHARED_SECRET
        mock_prop.return_value = ReplicationOutcome.AMBIGUOUS

        os.makedirs(SHARED_FILES_DIR, exist_ok=True)
        old_path = os.path.join(SHARED_FILES_DIR, "rename_ambig_old.txt")
        new_path = os.path.join(SHARED_FILES_DIR, "rename_ambig_new.txt")
        with open(old_path, "w") as f:
            f.write("rename test")
        if os.path.exists(new_path):
            os.remove(new_path)

        cmd = f"AUTH {TCP_SHARED_SECRET}\nRENAME rename_ambig_old.txt rename_ambig_new.txt\n".encode('utf-8')
        sock = MockSocket(cmd)
        handle_client(sock, ("127.0.0.1", 12345))

        # Ambiguous -> rolled back to old path!
        assert os.path.exists(old_path)
        assert not os.path.exists(new_path)
        assert b"ERROR REPLICATION_FAILED" in sock.sent_data

        # Clean up
        if os.path.exists(old_path):
            os.remove(old_path)

    @patch('replication._connect')
    def test_replication_ack_timeout(self, mock_connect):
        import socket
        class TimeoutMockSocket:
            def __init__(self):
                self.calls = 0
                self.closed = False
            def sendall(self, data):
                pass
            def recv(self, size):
                self.calls += 1
                if self.calls == 1:
                    return b"READY\n"
                else:
                    raise socket.timeout("timed out waiting for ack")
            def settimeout(self, t):
                pass
            def close(self):
                self.closed = True

        mock_socket = TimeoutMockSocket()
        mock_connect.return_value = mock_socket

        # Create a dummy file to replicate
        os.makedirs(SHARED_FILES_DIR, exist_ok=True)
        dummy_file = os.path.join(SHARED_FILES_DIR, "dummy_replicate.txt")
        with open(dummy_file, "w") as f:
            f.write("hello")

        from replication import replicate_file
        # Try to replicate — should return False (failure) and not raise/hang
        res = replicate_file("dummy_replicate.txt", dummy_file)
        assert res is False
        assert mock_socket.closed is True

        # Clean up
        if os.path.exists(dummy_file):
            os.remove(dummy_file)

    def test_tcp_auth_validation(self):
        from config import TCP_SHARED_SECRET
        
        # Test 7.1: Connecting without AUTH first
        sock = MockSocket(b"PING\n")
        handle_client(sock, ("127.0.0.1", 12345))
        assert b"ERROR UNAUTHORIZED" in sock.sent_data
        
        # Test 7.2: Connecting with invalid secret
        sock = MockSocket(b"AUTH wrongsecret\nPING\n")
        handle_client(sock, ("127.0.0.1", 12345))
        assert b"ERROR UNAUTHORIZED" in sock.sent_data
        
        # Test 7.3: Connecting with valid secret
        sock = MockSocket(f"AUTH {TCP_SHARED_SECRET}\nPING\n".encode('utf-8'))
        handle_client(sock, ("127.0.0.1", 12345))
        assert b"OK AUTHENTICATED" in sock.sent_data
        assert b"OK PONG" in sock.sent_data


class TestReplicaServer:
    def test_replica_write_restriction(self):
        from replica_server import handle_client as replica_handle_client
        from config import TCP_CLIENT_SECRET, TCP_REPLICATION_SECRET

        # Test 1: Authenticating with TCP_CLIENT_SECRET (read-only client role)
        # Mutating command DELETE should fail with ERROR WRITE_NOT_ALLOWED
        cmd_client = f"AUTH {TCP_CLIENT_SECRET}\nDELETE file.txt\n".encode('utf-8')
        sock = MockSocket(cmd_client)
        replica_handle_client(sock, ("127.0.0.1", 12345))
        assert b"OK AUTHENTICATED" in sock.sent_data
        assert b"ERROR WRITE_NOT_ALLOWED" in sock.sent_data

        # Mutating command RENAME should fail with ERROR WRITE_NOT_ALLOWED
        cmd_client_rename = f"AUTH {TCP_CLIENT_SECRET}\nRENAME old.txt new.txt\n".encode('utf-8')
        sock = MockSocket(cmd_client_rename)
        replica_handle_client(sock, ("127.0.0.1", 12345))
        assert b"ERROR WRITE_NOT_ALLOWED" in sock.sent_data

        # Mutating command REPLICATE should fail with ERROR WRITE_NOT_ALLOWED
        cmd_client_replicate = f"AUTH {TCP_CLIENT_SECRET}\nREPLICATE file.txt 100\n".encode('utf-8')
        sock = MockSocket(cmd_client_replicate)
        replica_handle_client(sock, ("127.0.0.1", 12345))
        assert b"ERROR WRITE_NOT_ALLOWED" in sock.sent_data

        # Test 2: Authenticating with TCP_REPLICATION_SECRET (replication role)
        # Mutating command DELETE should NOT return ERROR WRITE_NOT_ALLOWED (but rather ERROR FILE_NOT_FOUND)
        cmd_rep = f"AUTH {TCP_REPLICATION_SECRET}\nDELETE file.txt\n".encode('utf-8')
        sock = MockSocket(cmd_rep)
        replica_handle_client(sock, ("127.0.0.1", 12345))
        assert b"OK AUTHENTICATED" in sock.sent_data
        assert b"ERROR WRITE_NOT_ALLOWED" not in sock.sent_data
        assert b"ERROR FILE_NOT_FOUND" in sock.sent_data


