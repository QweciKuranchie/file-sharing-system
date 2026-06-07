import unittest
import os
import sqlite3
import jwt
from datetime import datetime, timedelta, timezone

import auth

TEST_DB = "test_fileshare.db"

class TestAuthModule(unittest.TestCase):
    def setUp(self):
        # Ensure test database is clean before each test
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)
        auth.init_db(TEST_DB)

    def tearDown(self):
        if os.path.exists(TEST_DB):
            try:
                os.remove(TEST_DB)
            except PermissionError:
                pass

    def test_database_initialization(self):
        # Verify tables exist
        conn = sqlite3.connect(TEST_DB)
        cursor = conn.cursor()
        
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users';")
        self.assertIsNotNone(cursor.fetchone())
        
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='files';")
        self.assertIsNotNone(cursor.fetchone())
        
        conn.close()

    def test_user_registration(self):
        # Register standard user
        user_id = auth.register_user("john_doe", "john@example.com", "securepassword", db_path=TEST_DB)
        self.assertIsInstance(user_id, int)
        
        # Verify duplicate username raises error
        with self.assertRaises(ValueError) as ctx:
            auth.register_user("john_doe", "john2@example.com", "anotherpwd", db_path=TEST_DB)
        self.assertIn("Username already exists", str(ctx.exception))
        
        # Verify duplicate email raises error
        with self.assertRaises(ValueError) as ctx:
            auth.register_user("other_user", "john@example.com", "anotherpwd", db_path=TEST_DB)
        self.assertIn("Email already exists", str(ctx.exception))

    def test_password_hashing(self):
        # Register user and fetch raw password hash from db
        auth.register_user("hash_test", "hash@example.com", "plain_password", db_path=TEST_DB)
        
        conn = sqlite3.connect(TEST_DB)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT password_hash FROM users WHERE username = 'hash_test'")
        row = cursor.fetchone()
        conn.close()
        
        stored_hash = row["password_hash"]
        self.assertNotIn("plain_password", stored_hash)
        self.assertTrue(auth.verify_password(stored_hash, "plain_password"))
        self.assertFalse(auth.verify_password(stored_hash, "wrong_password"))

    def test_user_login_success(self):
        auth.register_user("login_test", "login@example.com", "password123", db_path=TEST_DB)
        
        # Successful login returns JWT
        token = auth.login_user("login_test", "password123", db_path=TEST_DB)
        self.assertIsInstance(token, str)
        self.assertTrue(len(token) > 0)
        
        # Validate JWT returns user id
        user_id = auth.validate_jwt(token)
        self.assertIsInstance(user_id, int)

    def test_user_login_failure(self):
        auth.register_user("login_fail", "fail@example.com", "password123", db_path=TEST_DB)
        
        # Invalid password
        with self.assertRaises(ValueError) as ctx:
            auth.login_user("login_fail", "wrong_password", db_path=TEST_DB)
        self.assertIn("Invalid username or password", str(ctx.exception))
        
        # Invalid username
        with self.assertRaises(ValueError) as ctx:
            auth.login_user("nonexistent_user", "password123", db_path=TEST_DB)
        self.assertIn("Invalid username or password", str(ctx.exception))

    def test_jwt_validation_expired(self):
        # To test expiration, we can manually create an expired token using auth's secret
        payload = {
            "user_id": 42,
            "exp": datetime.now(timezone.utc) - timedelta(seconds=1)
        }
        expired_token = jwt.encode(payload, auth.JWT_SECRET, algorithm=auth.JWT_ALGORITHM)
        if isinstance(expired_token, bytes):
            expired_token = expired_token.decode('utf-8')
            
        with self.assertRaises(ValueError) as ctx:
            auth.validate_jwt(expired_token)
        self.assertIn("Token has expired", str(ctx.exception))

    def test_jwt_validation_tampered(self):
        auth.register_user("tamper_test", "tamper@example.com", "password", db_path=TEST_DB)
        token = auth.login_user("tamper_test", "password", db_path=TEST_DB)
        
        # Tamper token (change characters in payload/signature part)
        tampered_token = token[:-5] + "XXXXX"
        with self.assertRaises(ValueError) as ctx:
            auth.validate_jwt(tampered_token)
        self.assertIn("Invalid token", str(ctx.exception))

    def test_quota_checking_and_updating(self):
        user_id = auth.register_user("quota_test", "quota@example.com", "password", db_path=TEST_DB)
        
        # Default limit is 50MB (52428800 bytes). Default used is 0.
        # Check quota for 10MB (10485760 bytes)
        self.assertTrue(auth.check_quota(user_id, 10485760, db_path=TEST_DB))
        
        # Check quota for 51MB (should fail)
        self.assertFalse(auth.check_quota(user_id, 53477376, db_path=TEST_DB))
        
        # Update quota (add 30MB)
        auth.update_quota(user_id, 31457280, db_path=TEST_DB)
        
        # Check quota for another 20MB (should pass: 30MB + 20MB = 50MB <= 50MB)
        self.assertTrue(auth.check_quota(user_id, 20971520, db_path=TEST_DB))
        
        # Check quota for another 21MB (should fail: 30MB + 21MB = 51MB > 50MB)
        self.assertFalse(auth.check_quota(user_id, 22020096, db_path=TEST_DB))
        
        # Decrement quota (remove 15MB)
        auth.update_quota(user_id, -15728640, db_path=TEST_DB)
        
        # Check quota for another 20MB (used is now 15MB. 15MB + 20MB = 35MB <= 50MB)
        self.assertTrue(auth.check_quota(user_id, 20971520, db_path=TEST_DB))

    def test_automatic_db_initialization_on_first_run(self):
        # Clean up any existing DB file to test clean workspace
        TEMP_DB = "temp_first_run_test.db"
        if os.path.exists(TEMP_DB):
            try:
                os.remove(TEMP_DB)
            except PermissionError:
                pass
                
        try:
            # Call register_user directly without calling init_db.
            # This should automatically create the database and tables.
            user_id = auth.register_user("first_run_user", "first@example.com", "password123", db_path=TEMP_DB)
            self.assertIsInstance(user_id, int)
            
            # Verify DB file is created and has the expected table and record
            conn = sqlite3.connect(TEMP_DB)
            cursor = conn.cursor()
            cursor.execute("SELECT username FROM users WHERE id = ?", (user_id,))
            row = cursor.fetchone()
            self.assertEqual(row[0], "first_run_user")
            conn.close()
        finally:
            if os.path.exists(TEMP_DB):
                try:
                    os.remove(TEMP_DB)
                except PermissionError:
                    pass

    def test_concurrent_quota_reservation(self):
        import concurrent.futures
        
        # Register user with 50MB quota (default)
        user_id = auth.register_user("concurrent_test", "concurrent@example.com", "password", db_path=TEST_DB)
        
        # Limit is 52428800 bytes. We want to concurrently reserve 10MB 10 times.
        # Exactly 5 should succeed, 5 should fail.
        file_size = 10485760  # 10MB
        num_threads = 10
        
        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [
                executor.submit(auth.reserve_quota, user_id, file_size, TEST_DB)
                for _ in range(num_threads)
            ]
            for future in concurrent.futures.as_completed(futures):
                results.append(future.result())
                
        # Exactly 5 should have succeeded (True), and 5 should have failed (False)
        success_count = results.count(True)
        failure_count = results.count(False)
        
        self.assertEqual(success_count, 5)
        self.assertEqual(failure_count, 5)
        
        # Verify the actual stored value in DB is exactly 50MB
        conn = sqlite3.connect(TEST_DB)
        cursor = conn.cursor()
        cursor.execute("SELECT quota_used_bytes FROM users WHERE id = ?", (user_id,))
        row = cursor.fetchone()
        conn.close()
        self.assertEqual(row[0], 52428800)

    def test_update_quota_underflow(self):
        user_id = auth.register_user("underflow_test", "underflow@example.com", "password", db_path=TEST_DB)
        
        # Initial quota_used_bytes is 0. Attempting to decrement by 1 should fail.
        with self.assertRaises(ValueError) as ctx:
            auth.update_quota(user_id, -1, db_path=TEST_DB)
        self.assertIn("Quota used bytes cannot be negative", str(ctx.exception))
        
        # Increment to 10 bytes, then decrement by 15. Should fail.
        auth.update_quota(user_id, 10, db_path=TEST_DB)
        with self.assertRaises(ValueError) as ctx:
            auth.update_quota(user_id, -15, db_path=TEST_DB)
        self.assertIn("Quota used bytes cannot be negative", str(ctx.exception))
        
        # Quota should remain 10 bytes after the failed decrement
        conn = sqlite3.connect(TEST_DB)
        cursor = conn.cursor()
        cursor.execute("SELECT quota_used_bytes FROM users WHERE id = ?", (user_id,))
        row = cursor.fetchone()
        conn.close()
        self.assertEqual(row[0], 10)

    def test_update_quota_unknown_user(self):
        with self.assertRaises(ValueError) as ctx:
            auth.update_quota(9999, 10, db_path=TEST_DB)
        self.assertIn("User does not exist", str(ctx.exception))
        
        with self.assertRaises(ValueError) as ctx:
            auth.update_quota(9999, -10, db_path=TEST_DB)
        self.assertIn("User does not exist", str(ctx.exception))

    def test_reserve_quota_unknown_user_and_invalid_size(self):
        # Unknown user
        with self.assertRaises(ValueError) as ctx:
            auth.reserve_quota(9999, 10, db_path=TEST_DB)
        self.assertIn("User does not exist", str(ctx.exception))
        
        # Negative size
        user_id = auth.register_user("reserve_test", "reserve@example.com", "password", db_path=TEST_DB)
        with self.assertRaises(ValueError) as ctx:
            auth.reserve_quota(user_id, -5, db_path=TEST_DB)
        self.assertIn("File size cannot be negative", str(ctx.exception))

if __name__ == "__main__":
    unittest.main()
