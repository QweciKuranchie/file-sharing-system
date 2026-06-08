import unittest
import os
import sqlite3
import time
import tempfile
import auth
from auth import (
    register_user, login_user, decode_token, check_quota, 
    update_quota, reserve_quota, DB_PATH, get_db_connection, init_db
)

class TestAuthModule(unittest.TestCase):
    def setUp(self):
        # Create a temporary SQLite database
        self.db_fd, self.test_db_path = tempfile.mkstemp(suffix=".db")
        os.close(self.db_fd)
        
        # Override the database path in the auth module
        auth.set_db_path(self.test_db_path)
        
        # Initialize schema in the temporary database
        init_db()

    def tearDown(self):
        # Restore default path
        auth.set_db_path(auth.DEFAULT_DB_PATH)
        # Delete temporary database file
        try:
            if os.path.exists(self.test_db_path):
                os.remove(self.test_db_path)
        except OSError:
            pass

    def test_user_registration_and_login(self):
        # Test successful registration
        user_id = register_user("testuser", "test@example.com", "securepassword")
        self.assertIsNotNone(user_id)
        
        # Test duplicate username
        with self.assertRaises(ValueError) as ctx:
            register_user("testuser", "other@example.com", "anotherpassword")
        self.assertIn("Username is already taken", str(ctx.exception))
        
        # Test duplicate email
        with self.assertRaises(ValueError) as ctx:
            register_user("otheruser", "test@example.com", "anotherpassword")
        self.assertIn("Email is already registered", str(ctx.exception))
        
        # Test login success
        token = login_user("testuser", "securepassword")
        self.assertIsNotNone(token)
        
        # Test login via email
        token_email = login_user("test@example.com", "securepassword")
        self.assertIsNotNone(token_email)
        
        # Test login failure (invalid password)
        with self.assertRaises(ValueError):
            login_user("testuser", "wrongpassword")
            
        # Test login failure (non-existent user)
        with self.assertRaises(ValueError):
            login_user("nonexistent", "password")

    def test_jwt_tokens(self):
        user_id = register_user("jwtuser", "jwt@example.com", "password")
        token = login_user("jwtuser", "password")
        
        # Decode valid token
        payload = decode_token(token)
        self.assertEqual(payload["user_id"], user_id)
        
        # Test invalid token format
        with self.assertRaises(ValueError):
            decode_token("invalid.token.format")
            
        # Test tampered token signature
        parts = token.split('.')
        parts[2] = parts[2] + "tamper"
        tampered_token = ".".join(parts)
        with self.assertRaises(ValueError) as ctx:
            decode_token(tampered_token)
        self.assertIn("Signature verification failed", str(ctx.exception))

    def test_quota_management(self):
        user_id = register_user("quotauser", "quota@example.com", "password")
        
        # Default quota is 50MB (52428800 bytes)
        self.assertTrue(check_quota(user_id, 10 * 1024 * 1024)) # 10MB fits
        self.assertTrue(check_quota(user_id, 50 * 1024 * 1024)) # 50MB fits
        self.assertFalse(check_quota(user_id, 51 * 1024 * 1024)) # 51MB fails
        
        # Test atomic quota reservation
        reserved = reserve_quota(user_id, 30 * 1024 * 1024) # reserve 30MB
        self.assertTrue(reserved)
        
        # Check remaining quota: 20MB left
        self.assertTrue(check_quota(user_id, 20 * 1024 * 1024))
        self.assertFalse(check_quota(user_id, 21 * 1024 * 1024))
        
        # Reserve another 25MB (should fail, only 20MB left)
        reserved_fail = reserve_quota(user_id, 25 * 1024 * 1024)
        self.assertFalse(reserved_fail)
        
        # Update quota (decrement 10MB, e.g. deleting a file)
        update_quota(user_id, -10 * 1024 * 1024)
        
        # Now 30MB is left (50MB - 30MB reserved + 10MB freed = 30MB available)
        self.assertTrue(check_quota(user_id, 30 * 1024 * 1024))
        self.assertFalse(check_quota(user_id, 31 * 1024 * 1024))

    def test_quota_underflow_and_nonexistent_user(self):
        user_id = register_user("underflowuser", "underflow@example.com", "password")
        
        # Test quota underflow check (can't go below 0)
        with self.assertRaises(ValueError) as ctx:
            update_quota(user_id, -10)
        self.assertIn("underflow", str(ctx.exception).lower())
        
        # Test nonexistent user on quota checks
        with self.assertRaises(ValueError):
            check_quota(99999, 100)
            
        with self.assertRaises(ValueError):
            reserve_quota(99999, 100)
            
        with self.assertRaises(ValueError):
            update_quota(99999, 100)

if __name__ == "__main__":
    unittest.main()
