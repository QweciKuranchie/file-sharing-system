import unittest
import os
import sqlite3
import tempfile
from flask import session
import auth
from app import app

class TestAppRoutes(unittest.TestCase):
    def setUp(self):
        # Configure app for testing
        app.config['TESTING'] = True
        app.config['SECRET_KEY'] = 'test-secret-key'
        self.client = app.test_client()
        
        # Create a temporary SQLite database
        self.db_fd, self.test_db_path = tempfile.mkstemp(suffix=".db")
        os.close(self.db_fd)
        
        # Override database path in auth module
        auth.set_db_path(self.test_db_path)
        
        # Initialize schema
        auth.init_db()

    def tearDown(self):
        # Restore default path
        auth.set_db_path(auth.DEFAULT_DB_PATH)
        # Delete temporary database file
        try:
            if os.path.exists(self.test_db_path):
                os.remove(self.test_db_path)
        except OSError:
            pass

    def test_unauthenticated_redirects(self):
        # Unauthenticated access to dashboard redirects to login
        response = self.client.get('/dashboard', follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response.location)
        
        # Unauthenticated access to profile redirects to login
        response = self.client.get('/profile', follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response.location)

    def test_registration_and_login_flow(self):
        # 1. Register a new user
        response = self.client.post('/register', data={
            'username': 'john_doe',
            'email': 'john@example.com',
            'password': 'password123',
            'confirm_password': 'password123'
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Registration successful! Please login.", response.data)
        
        # 2. Register with duplicate username
        response = self.client.post('/register', data={
            'username': 'john_doe',
            'email': 'different@example.com',
            'password': 'password123',
            'confirm_password': 'password123'
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Username is already taken.", response.data)
        
        # 3. Register with duplicate email
        response = self.client.post('/register', data={
            'username': 'other_john',
            'email': 'john@example.com',
            'password': 'password123',
            'confirm_password': 'password123'
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Email is already registered.", response.data)
        
        # 4. Register with mismatched passwords
        response = self.client.post('/register', data={
            'username': 'new_user',
            'email': 'new@example.com',
            'password': 'password123',
            'confirm_password': 'mismatched'
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Passwords do not match.", response.data)

        # 5. Login with invalid credentials
        response = self.client.post('/login', data={
            'username': 'john_doe',
            'password': 'wrong_password'
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Invalid username/email or password.", response.data)
        
        # 6. Login with valid credentials
        with self.client:
            response = self.client.post('/login', data={
                'username': 'john_doe',
                'password': 'password123'
            }, follow_redirects=False)
            self.assertEqual(response.status_code, 302)
            self.assertIn('/dashboard', response.location)
            self.assertIn('token', session)
            
            # Access dashboard and verify access
            response = self.client.get('/dashboard')
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"john_doe", response.data)
            
            # Access profile and verify quota details
            response = self.client.get('/profile')
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"User Profile", response.data)
            self.assertIn(b"john_doe", response.data)
            
            # 7. Logout
            response = self.client.get('/logout', follow_redirects=True)
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"You have been logged out.", response.data)
            self.assertNotIn('token', session)

    def test_session_expiration(self):
        # Set an invalid/expired token manually in the session
        with self.client as c:
            with c.session_transaction() as sess:
                sess['token'] = 'invalid.header.signature'
            
            # Accessing dashboard with expired/invalid token should redirect silently to /login
            response = c.get('/dashboard', follow_redirects=False)
            self.assertEqual(response.status_code, 302)
            self.assertIn('/login', response.location)
            # The token should be popped from session
            self.assertNotIn('token', session)

if __name__ == '__main__':
    unittest.main()
