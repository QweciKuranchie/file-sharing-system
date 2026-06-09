import unittest
import os
import sqlite3
import tempfile
import socket

# Override database path in config BEFORE importing app or database
import config
_TEST_DB_FD, _TEST_DB_PATH = tempfile.mkstemp(suffix=".db")
os.close(_TEST_DB_FD)
config.DATABASE_PATH = _TEST_DB_PATH

from flask import session
from unittest.mock import patch, MagicMock
import auth
from database import init_db
from app import app

class SocketMock:
    def __init__(self, response_bytes, raise_on_connect=None):
        self.response_bytes = response_bytes
        self.raise_on_connect = raise_on_connect
        self.index = 0
        self.sent_data = bytearray()
        self.closed = False
        self.addr = None

    def connect(self, addr):
        self.addr = addr
        if self.raise_on_connect:
            raise self.raise_on_connect

    def settimeout(self, t):
        pass

    def sendall(self, data):
        self.sent_data.extend(data)

    def recv(self, num_bytes):
        if self.index >= len(self.response_bytes):
            return b''
        chunk = self.response_bytes[self.index : self.index + num_bytes]
        self.index += len(chunk)
        return chunk

    def close(self):
        self.closed = True


# Ensure database tables exist
init_db()

class TestAppRoutes(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        # Delete temporary database file and its WAL logs
        try:
            if os.path.exists(_TEST_DB_PATH):
                os.remove(_TEST_DB_PATH)
            for suffix in ["-wal", "-shm"]:
                wal_path = _TEST_DB_PATH + suffix
                if os.path.exists(wal_path):
                    os.remove(wal_path)
        except OSError:
            pass

    def setUp(self):
        # Configure app for testing
        app.config['TESTING'] = True
        app.config['SECRET_KEY'] = 'test-secret-key'
        self.client = app.test_client()
        
        # Clear tables to ensure fresh state for each test
        from database import get_connection
        conn = get_connection()
        try:
            conn.execute("DELETE FROM users")
            conn.execute("DELETE FROM files")
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
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

    @patch('app.socket.socket')
    def test_upload_success(self, mock_socket_class):
        self.client.post('/register', data={
            'username': 'uploader',
            'email': 'uploader@example.com',
            'password': 'password123',
            'confirm_password': 'password123'
        })
        self.client.post('/login', data={
            'username': 'uploader',
            'password': 'password123'
        })
        
        sock_ping = SocketMock(b"OK PONG\n")
        sock_upload = SocketMock(b"READY\nOK FILE_SAVED uploader_file.txt\n")
        mock_socket_class.side_effect = [sock_ping, sock_upload]
        
        data = {
            'file': (tempfile.SpooledTemporaryFile(), 'test file.txt')
        }
        data['file'][0].write(b"Hello from test file")
        data['file'][0].seek(0)
        
        response = self.client.post('/upload', data=data, content_type='multipart/form-data')
        self.assertEqual(response.status_code, 200)
        json_data = response.get_json()
        self.assertEqual(json_data['status'], 'success')
        self.assertEqual(json_data['filename'], 'uploader_file.txt')
        
        file_record = auth.get_file_by_name('uploader_file.txt')
        self.assertIsNotNone(file_record)
        self.assertEqual(file_record['original_name'], 'test file.txt')
        self.assertEqual(file_record['file_size_bytes'], 20)
        
        user = auth.get_user_by_id(file_record['owner_id'])
        self.assertIsNotNone(user)
        self.assertEqual(user['quota_used_bytes'], 20)

    @patch('app.socket.socket')
    def test_upload_replication_warning(self, mock_socket_class):
        self.client.post('/register', data={
            'username': 'warnuser',
            'email': 'warn@example.com',
            'password': 'password123',
            'confirm_password': 'password123'
        })
        self.client.post('/login', data={
            'username': 'warnuser',
            'password': 'password123'
        })
        
        sock_ping = SocketMock(b"OK PONG\n")
        sock_upload = SocketMock(b"READY\nERROR REPLICATION_FAILED\n")
        mock_socket_class.side_effect = [sock_ping, sock_upload]
        
        data = {
            'file': (tempfile.SpooledTemporaryFile(), 'warn_file.txt')
        }
        data['file'][0].write(b"warn content")
        data['file'][0].seek(0)
        
        response = self.client.post('/upload', data=data, content_type='multipart/form-data')
        self.assertEqual(response.status_code, 207)
        json_data = response.get_json()
        self.assertEqual(json_data['status'], 'warning')
        self.assertIn("replication failed", json_data['message'])

    @patch('app.socket.socket')
    def test_upload_quota_exceeded(self, mock_socket_class):
        self.client.post('/register', data={
            'username': 'quota_user',
            'email': 'quota_u@example.com',
            'password': 'password123',
            'confirm_password': 'password123'
        })
        self.client.post('/login', data={
            'username': 'quota_user',
            'password': 'password123'
        })
        
        sock_ping = SocketMock(b"OK PONG\n")
        mock_socket_class.side_effect = [sock_ping]
        
        user_id = auth.login_user('quota_user', 'password123')
        payload = auth.decode_token(user_id)
        u_id = payload['user_id']
        auth.update_quota(u_id, 52428800 - 10)
        
        data = {
            'file': (tempfile.SpooledTemporaryFile(), 'test_quota.txt')
        }
        data['file'][0].write(b"01234567890123456789")
        data['file'][0].seek(0)
        
        response = self.client.post('/upload', data=data, content_type='multipart/form-data')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['status'], 'error')
        self.assertIn("quota exceeded", response.get_json()['message'])

    @patch('app.socket.socket')
    def test_upload_too_large(self, mock_socket_class):
        self.client.post('/register', data={
            'username': 'largeuser',
            'email': 'large@example.com',
            'password': 'password123',
            'confirm_password': 'password123'
        })
        self.client.post('/login', data={
            'username': 'largeuser',
            'password': 'password123'
        })
        
        sock_ping = SocketMock(b"OK PONG\n")
        mock_socket_class.side_effect = [sock_ping]
        
        data = {
            'file': (tempfile.SpooledTemporaryFile(), 'large.txt')
        }
        data['file'][0].write(b"a" * (10 * 1024 * 1024 + 1))
        data['file'][0].seek(0)
        
        response = self.client.post('/upload', data=data, content_type='multipart/form-data')
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.get_json()['status'], 'error')
        self.assertIn("exceeds 10MB", response.get_json()['message'])

    @patch('app.socket.socket')
    def test_upload_failover_write_disallowed(self, mock_socket_class):
        self.client.post('/register', data={
            'username': 'failover_user',
            'email': 'failover@example.com',
            'password': 'password123',
            'confirm_password': 'password123'
        })
        self.client.post('/login', data={
            'username': 'failover_user',
            'password': 'password123'
        })
        
        sock_ping = SocketMock(b"", raise_on_connect=ConnectionRefusedError("Offline"))
        mock_socket_class.side_effect = [sock_ping]
        
        data = {
            'file': (tempfile.SpooledTemporaryFile(), 'test_failover.txt')
        }
        data['file'][0].write(b"test data")
        data['file'][0].seek(0)
        
        response = self.client.post('/upload', data=data, content_type='multipart/form-data')
        self.assertEqual(response.status_code, 503)
        self.assertIn("unavailable in failover mode", response.get_json()['message'])

    @patch('app.socket.socket')
    def test_download_success(self, mock_socket_class):
        self.client.post('/register', data={
            'username': 'downloader',
            'email': 'downloader@example.com',
            'password': 'password123',
            'confirm_password': 'password123'
        })
        self.client.post('/login', data={
            'username': 'downloader',
            'password': 'password123'
        })
        
        user_id = auth.login_user('downloader', 'password123')
        u_payload = auth.decode_token(user_id)
        u_id = u_payload['user_id']
        
        auth.add_file('stored_name.txt', 'original_name.txt', 'document', 11, u_id)
        
        sock_ping = SocketMock(b"OK PONG\n")
        sock_download = SocketMock(b"OK 11\nhello world")
        mock_socket_class.side_effect = [sock_ping, sock_download]
        
        response = self.client.get('/download/stored_name.txt')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"hello world")
        self.assertEqual(response.headers['Content-Length'], '11')
        self.assertIn('attachment; filename="original_name.txt"', response.headers['Content-Disposition'])

    @patch('app.socket.socket')
    def test_download_failover_to_replica(self, mock_socket_class):
        self.client.post('/register', data={
            'username': 'downloader_failover',
            'email': 'dfail@example.com',
            'password': 'password123',
            'confirm_password': 'password123'
        })
        self.client.post('/login', data={
            'username': 'downloader_failover',
            'password': 'password123'
        })
        
        user_id = auth.login_user('downloader_failover', 'password123')
        u_payload = auth.decode_token(user_id)
        u_id = u_payload['user_id']
        auth.add_file('failover.txt', 'orig_failover.txt', 'document', 11, u_id)
        
        sock_ping = SocketMock(b"", raise_on_connect=ConnectionRefusedError("Offline"))
        sock_download = SocketMock(b"OK 12\nreplica data")
        mock_socket_class.side_effect = [sock_ping, sock_download]
        
        response = self.client.get('/download/failover.txt')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"replica data")

    @patch('app.socket.socket')
    def test_delete_success(self, mock_socket_class):
        self.client.post('/register', data={
            'username': 'deleter',
            'email': 'deleter@example.com',
            'password': 'password123',
            'confirm_password': 'password123'
        })
        self.client.post('/login', data={
            'username': 'deleter',
            'password': 'password123'
        })
        
        user_id = auth.login_user('deleter', 'password123')
        u_payload = auth.decode_token(user_id)
        u_id = u_payload['user_id']
        auth.add_file('delete_me.txt', 'delete_me.txt', 'document', 15, u_id)
        auth.update_quota(u_id, 15)
        
        sock_ping = SocketMock(b"OK PONG\n")
        sock_delete = SocketMock(b"OK FILE_DELETED\n")
        mock_socket_class.side_effect = [sock_ping, sock_delete]
        
        response = self.client.post('/delete/delete_me.txt')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['status'], 'success')
        
        self.assertIsNone(auth.get_file_by_name('delete_me.txt'))
        
        user = auth.get_user_by_id(u_id)
        self.assertIsNotNone(user)
        self.assertEqual(user['quota_used_bytes'], 0)

    @patch('app.socket.socket')
    def test_delete_not_owned(self, mock_socket_class):
        self.client.post('/register', data={
            'username': 'owner_user',
            'email': 'owner@example.com',
            'password': 'password123',
            'confirm_password': 'password123'
        })
        user1_id = auth.login_user('owner_user', 'password123')
        u1_payload = auth.decode_token(user1_id)
        u1_id = u1_payload['user_id']
        auth.add_file('other_file.txt', 'other_file.txt', 'document', 10, u1_id)
        
        self.client.post('/register', data={
            'username': 'other_user',
            'email': 'other@example.com',
            'password': 'password123',
            'confirm_password': 'password123'
        })
        self.client.post('/login', data={
            'username': 'other_user',
            'password': 'password123'
        })
        
        sock_ping = SocketMock(b"OK PONG\n")
        mock_socket_class.side_effect = [sock_ping]
        
        response = self.client.post('/delete/other_file.txt')
        self.assertEqual(response.status_code, 403)
        self.assertIn("delete files you own", response.get_json()['message'])

    @patch('app.socket.socket')
    def test_rename_success(self, mock_socket_class):
        self.client.post('/register', data={
            'username': 'renamer',
            'email': 'renamer@example.com',
            'password': 'password123',
            'confirm_password': 'password123'
        })
        self.client.post('/login', data={
            'username': 'renamer',
            'password': 'password123'
        })
        
        user_id = auth.login_user('renamer', 'password123')
        u_payload = auth.decode_token(user_id)
        u_id = u_payload['user_id']
        auth.add_file('old_name.txt', 'old_name.txt', 'document', 10, u_id)
        
        sock_ping = SocketMock(b"OK PONG\n")
        sock_rename = SocketMock(b"OK FILE_RENAMED renamed.txt\n")
        mock_socket_class.side_effect = [sock_ping, sock_rename]
        
        response = self.client.post('/rename/old_name.txt', data={'new_name': 'renamed.txt'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['status'], 'success')
        self.assertEqual(response.get_json()['message'], 'File renamed to renamed.txt')
        
        self.assertIsNone(auth.get_file_by_name('old_name.txt'))
        new_record = auth.get_file_by_name('renamed.txt')
        self.assertIsNotNone(new_record)
        self.assertEqual(new_record['original_name'], 'renamed.txt')

    def test_list_files(self):
        self.client.post('/register', data={
            'username': 'lister',
            'email': 'lister@example.com',
            'password': 'password123',
            'confirm_password': 'password123'
        })
        self.client.post('/login', data={
            'username': 'lister',
            'password': 'password123'
        })
        
        user_id = auth.login_user('lister', 'password123')
        u_payload = auth.decode_token(user_id)
        u_id = u_payload['user_id']
        
        auth.add_file('file1.pdf', 'file1.pdf', 'pdf', 204800, u_id)
        auth.add_file('file2.png', 'file2.png', 'image', 1024, u_id)
        
        response = self.client.get('/files')
        self.assertEqual(response.status_code, 200)
        
        json_data = response.get_json()
        files = json_data['files']
        self.assertEqual(len(files), 2)
        
        self.assertEqual(files[0]['name'], 'file2.png')
        self.assertEqual(files[0]['size'], '1.0 KB')
        self.assertEqual(files[0]['type'], 'image')
        self.assertEqual(files[0]['owner'], 'lister')
        
        self.assertEqual(files[1]['name'], 'file1.pdf')
        self.assertEqual(files[1]['size'], '200.0 KB')
        self.assertEqual(files[1]['type'], 'pdf')

    @patch('app.socket.socket')
    @patch('auth.add_file')
    def test_upload_sqlite_failure_compensation(self, mock_add_file, mock_socket_class):
        self.client.post('/register', data={
            'username': 'compuser',
            'email': 'comp@example.com',
            'password': 'password123',
            'confirm_password': 'password123'
        })
        self.client.post('/login', data={
            'username': 'compuser',
            'password': 'password123'
        })
        
        user_id = auth.login_user('compuser', 'password123')
        u_payload = auth.decode_token(user_id)
        u_id = u_payload['user_id']
        
        # Verify quota is initially 0
        user_before = auth.get_user_by_id(u_id)
        self.assertIsNotNone(user_before)
        self.assertEqual(user_before['quota_used_bytes'], 0)
        
        # Set auth.add_file to raise an error
        mock_add_file.side_effect = Exception("SQLite Database Failure")
        
        sock_ping = SocketMock(b"OK PONG\n")
        sock_upload = SocketMock(b"READY\nOK FILE_SAVED comp_file.txt\n")
        sock_delete = SocketMock(b"OK FILE_DELETED\n")
        mock_socket_class.side_effect = [sock_ping, sock_upload, sock_delete]
        
        data = {
            'file': (tempfile.SpooledTemporaryFile(), 'comp_file.txt')
        }
        data['file'][0].write(b"some content")
        data['file'][0].seek(0)
        
        response = self.client.post('/upload', data=data, content_type='multipart/form-data')
        self.assertEqual(response.status_code, 500)
        
        # Check that quota was rolled back to 0
        user_after = auth.get_user_by_id(u_id)
        self.assertIsNotNone(user_after)
        self.assertEqual(user_after['quota_used_bytes'], 0)
        
        # Verify that DELETE command was sent to the TCP server
        self.assertIn(b"DELETE comp_file.txt", sock_delete.sent_data)

    @patch('app.socket.socket')
    @patch('auth.add_file')
    def test_upload_replicate_warning_sqlite_failure_compensation(self, mock_add_file, mock_socket_class):
        self.client.post('/register', data={
            'username': 'compuser2',
            'email': 'comp2@example.com',
            'password': 'password123',
            'confirm_password': 'password123'
        })
        self.client.post('/login', data={
            'username': 'compuser2',
            'password': 'password123'
        })
        
        user_id = auth.login_user('compuser2', 'password123')
        u_payload = auth.decode_token(user_id)
        u_id = u_payload['user_id']
        
        # Verify quota is initially 0
        user_before = auth.get_user_by_id(u_id)
        self.assertIsNotNone(user_before)
        self.assertEqual(user_before['quota_used_bytes'], 0)
        
        # Set auth.add_file to raise an error
        mock_add_file.side_effect = Exception("SQLite Database Failure")
        
        sock_ping = SocketMock(b"OK PONG\n")
        sock_upload = SocketMock(b"READY\nERROR REPLICATION_FAILED\n")
        sock_delete = SocketMock(b"OK FILE_DELETED\n")
        mock_socket_class.side_effect = [sock_ping, sock_upload, sock_delete]
        
        data = {
            'file': (tempfile.SpooledTemporaryFile(), 'comp2_file.txt')
        }
        data['file'][0].write(b"some content")
        data['file'][0].seek(0)
        
        response = self.client.post('/upload', data=data, content_type='multipart/form-data')
        self.assertEqual(response.status_code, 500)
        
        # Check that quota was rolled back to 0
        user_after = auth.get_user_by_id(u_id)
        self.assertIsNotNone(user_after)
        self.assertEqual(user_after['quota_used_bytes'], 0)
        
        # Verify that DELETE command was sent to the TCP server
        self.assertIn(b"DELETE comp2_file.txt", sock_delete.sent_data)

    @patch('app.socket.socket')
    def test_delete_file_not_found(self, mock_socket_class):
        self.client.post('/register', data={
            'username': 'deleter2',
            'email': 'deleter2@example.com',
            'password': 'password123',
            'confirm_password': 'password123'
        })
        self.client.post('/login', data={
            'username': 'deleter2',
            'password': 'password123'
        })
        
        user_id = auth.login_user('deleter2', 'password123')
        u_payload = auth.decode_token(user_id)
        u_id = u_payload['user_id']
        auth.add_file('not_found.txt', 'not_found.txt', 'document', 15, u_id)
        
        sock_ping = SocketMock(b"OK PONG\n")
        sock_delete = SocketMock(b"ERROR FILE_NOT_FOUND\n")
        mock_socket_class.side_effect = [sock_ping, sock_delete]
        
        response = self.client.post('/delete/not_found.txt')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()['status'], 'error')
        self.assertEqual(response.get_json()['message'], 'File not found')

    @patch('app.socket.socket')
    def test_delete_failed_server_error(self, mock_socket_class):
        self.client.post('/register', data={
            'username': 'deleter3',
            'email': 'deleter3@example.com',
            'password': 'password123',
            'confirm_password': 'password123'
        })
        self.client.post('/login', data={
            'username': 'deleter3',
            'password': 'password123'
        })
        
        user_id = auth.login_user('deleter3', 'password123')
        u_payload = auth.decode_token(user_id)
        u_id = u_payload['user_id']
        auth.add_file('delete_fail.txt', 'delete_fail.txt', 'document', 15, u_id)
        
        sock_ping = SocketMock(b"OK PONG\n")
        sock_delete = SocketMock(b"ERROR DELETE_FAILED\n")
        mock_socket_class.side_effect = [sock_ping, sock_delete]
        
        response = self.client.post('/delete/delete_fail.txt')
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json()['status'], 'error')
        self.assertIn('Delete failed', response.get_json()['message'])

    @patch('app.socket.socket')
    def test_rename_file_not_found(self, mock_socket_class):
        self.client.post('/register', data={
            'username': 'renamer2',
            'email': 'renamer2@example.com',
            'password': 'password123',
            'confirm_password': 'password123'
        })
        self.client.post('/login', data={
            'username': 'renamer2',
            'password': 'password123'
        })
        
        user_id = auth.login_user('renamer2', 'password123')
        u_payload = auth.decode_token(user_id)
        u_id = u_payload['user_id']
        auth.add_file('not_found.txt', 'not_found.txt', 'document', 10, u_id)
        
        sock_ping = SocketMock(b"OK PONG\n")
        sock_rename = SocketMock(b"ERROR FILE_NOT_FOUND\n")
        mock_socket_class.side_effect = [sock_ping, sock_rename]
        
        response = self.client.post('/rename/not_found.txt', data={'new_name': 'renamed.txt'})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()['status'], 'error')
        self.assertEqual(response.get_json()['message'], 'File not found')

    @patch('app.socket.socket')
    def test_rename_conflict(self, mock_socket_class):
        self.client.post('/register', data={
            'username': 'renamer3',
            'email': 'renamer3@example.com',
            'password': 'password123',
            'confirm_password': 'password123'
        })
        self.client.post('/login', data={
            'username': 'renamer3',
            'password': 'password123'
        })
        
        user_id = auth.login_user('renamer3', 'password123')
        u_payload = auth.decode_token(user_id)
        u_id = u_payload['user_id']
        auth.add_file('conflict.txt', 'conflict.txt', 'document', 10, u_id)
        
        sock_ping = SocketMock(b"OK PONG\n")
        sock_rename = SocketMock(b"ERROR NAME_CONFLICT\n")
        mock_socket_class.side_effect = [sock_ping, sock_rename]
        
        response = self.client.post('/rename/conflict.txt', data={'new_name': 'renamed.txt'})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()['status'], 'error')
        self.assertEqual(response.get_json()['message'], 'A file with that name already exists')

    @patch('app.socket.socket')
    def test_rename_failed_server_error(self, mock_socket_class):
        self.client.post('/register', data={
            'username': 'renamer4',
            'email': 'renamer4@example.com',
            'password': 'password123',
            'confirm_password': 'password123'
        })
        self.client.post('/login', data={
            'username': 'renamer4',
            'password': 'password123'
        })
        
        user_id = auth.login_user('renamer4', 'password123')
        u_payload = auth.decode_token(user_id)
        u_id = u_payload['user_id']
        auth.add_file('rename_fail.txt', 'rename_fail.txt', 'document', 10, u_id)
        
        sock_ping = SocketMock(b"OK PONG\n")
        sock_rename = SocketMock(b"ERROR RENAME_FAILED\n")
        mock_socket_class.side_effect = [sock_ping, sock_rename]
        
        response = self.client.post('/rename/rename_fail.txt', data={'new_name': 'renamed.txt'})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json()['status'], 'error')
        self.assertIn('Rename failed', response.get_json()['message'])

if __name__ == '__main__':
    unittest.main()

