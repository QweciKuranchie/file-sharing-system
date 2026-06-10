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
    def __init__(self, response_bytes, raise_on_connect=None, skip_auth_prefix=False):
        if not skip_auth_prefix and not raise_on_connect:
            self.response_bytes = b"OK AUTHENTICATED\n" + response_bytes
        else:
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


class DelayedBodySocketMock:
    def __init__(self, response_bytes, skip_auth_prefix=False):
        self.skip_auth_prefix = skip_auth_prefix
        if not skip_auth_prefix:
            self.response_bytes = b"OK AUTHENTICATED\n" + response_bytes
        else:
            self.response_bytes = response_bytes
        self.index = 0
        self.sent_data = bytearray()
        self.closed = False
        self.timeout = None
        self.header_read_done = False
        self.newlines_seen = 0

    def connect(self, addr):
        pass

    def settimeout(self, t):
        self.timeout = t

    def sendall(self, data):
        self.sent_data.extend(data)

    def recv(self, num_bytes):
        if self.index >= len(self.response_bytes):
            return b''
            
        if self.header_read_done:
            if self.timeout is not None:
                raise socket.timeout("unintended timeout: timeout remained 5s during body transfer")
        
        chunk = self.response_bytes[self.index : self.index + num_bytes]
        self.index += len(chunk)
        
        self.newlines_seen += chunk.count(b'\n')
        target_newlines = 1 if self.skip_auth_prefix else 2
        if self.newlines_seen >= target_newlines and not self.header_read_done:
            self.header_read_done = True
            
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

        # Verify that the uploaded file appears in the dashboard page (FE-2 metadata/actions check)
        response = self.client.get('/dashboard')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"test file.txt", response.data)
        self.assertIn(b"uploader_file.txt", response.data)
        self.assertIn(b"download-btn-uploader_file.txt", response.data)
        self.assertIn(b"rename-btn-uploader_file.txt", response.data)
        self.assertIn(b"delete-btn-uploader_file.txt", response.data)

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
    def test_download_primary_connect_fails_failover_to_replica(self, mock_socket_class):
        self.client.post('/register', data={
            'username': 'dl_conn_fail',
            'email': 'dl_conn_fail@example.com',
            'password': 'password123',
            'confirm_password': 'password123'
        })
        self.client.post('/login', data={
            'username': 'dl_conn_fail',
            'password': 'password123'
        })
        
        user_id = auth.login_user('dl_conn_fail', 'password123')
        u_payload = auth.decode_token(user_id)
        u_id = u_payload['user_id']
        auth.add_file('conn_fail.txt', 'orig_conn_fail.txt', 'document', 12, u_id)
        
        # Reset the global failover state first
        import app as app_module
        app_module.PRIMARY_DOWN = False
        
        # 1. PING succeeds (1st socket)
        sock_ping = SocketMock(b"OK PONG\n")
        # 2. Primary download connection fails (2nd socket)
        sock_primary = SocketMock(b"", raise_on_connect=ConnectionRefusedError("Offline"))
        # 3. Replica download succeeds (3rd socket)
        sock_replica = SocketMock(b"OK 12\nreplica data")
        
        mock_socket_class.side_effect = [sock_ping, sock_primary, sock_replica]
        
        response = self.client.get('/download/conn_fail.txt')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"replica data")
        self.assertTrue(app_module.PRIMARY_DOWN)

    @patch('app.socket.socket')
    def test_download_primary_header_read_fails_failover_to_replica(self, mock_socket_class):
        self.client.post('/register', data={
            'username': 'dl_hdr_fail',
            'email': 'dl_hdr_fail@example.com',
            'password': 'password123',
            'confirm_password': 'password123'
        })
        self.client.post('/login', data={
            'username': 'dl_hdr_fail',
            'password': 'password123'
        })
        
        user_id = auth.login_user('dl_hdr_fail', 'password123')
        u_payload = auth.decode_token(user_id)
        u_id = u_payload['user_id']
        auth.add_file('hdr_fail.txt', 'orig_hdr_fail.txt', 'document', 12, u_id)
        
        # Reset the global failover state first
        import app as app_module
        app_module.PRIMARY_DOWN = False
        
        # 1. PING succeeds (1st socket)
        sock_ping = SocketMock(b"OK PONG\n")
        # 2. Primary download connection succeeds, but reading header returns empty/fails (2nd socket)
        sock_primary = SocketMock(b"")
        # 3. Replica download succeeds (3rd socket)
        sock_replica = SocketMock(b"OK 12\nreplica data")
        
        mock_socket_class.side_effect = [sock_ping, sock_primary, sock_replica]
        
        response = self.client.get('/download/hdr_fail.txt')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"replica data")
        self.assertTrue(app_module.PRIMARY_DOWN)

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

    def test_dashboard_file_listing(self):
        # Register and login a user
        self.client.post('/register', data={
            'username': 'dash_user',
            'email': 'dash@example.com',
            'password': 'password123',
            'confirm_password': 'password123'
        })
        self.client.post('/login', data={
            'username': 'dash_user',
            'password': 'password123'
        })
        
        user_id = auth.login_user('dash_user', 'password123')
        payload = auth.decode_token(user_id)
        u_id = payload['user_id']
        
        # Add test files to DB
        auth.add_file('dash_stored.pdf', 'My Cloud File.pdf', 'pdf', 1024, u_id)
        
        # Get dashboard
        response = self.client.get('/dashboard')
        self.assertEqual(response.status_code, 200)
        
        # Verify metadata and action entry points appear in the HTML
        self.assertIn(b"My Cloud File.pdf", response.data)
        self.assertIn(b"1.0 KB", response.data)
        self.assertIn(b"dash_stored.pdf", response.data)
        self.assertIn(b"download-btn-dash_stored.pdf", response.data)
        self.assertIn(b"rename-btn-dash_stored.pdf", response.data)
        self.assertIn(b"delete-btn-dash_stored.pdf", response.data)

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
    @patch('auth.add_file')
    def test_upload_failed_compensating_delete_retains_quota(self, mock_add_file, mock_socket_class):
        """When add_file raises and the compensating DELETE also fails,
        quota must NOT be released — the file still exists on disk."""
        self.client.post('/register', data={
            'username': 'comp_del_fail',
            'email': 'comp_del_fail@example.com',
            'password': 'password123',
            'confirm_password': 'password123'
        })
        self.client.post('/login', data={
            'username': 'comp_del_fail',
            'password': 'password123'
        })

        user_id = auth.login_user('comp_del_fail', 'password123')
        u_payload = auth.decode_token(user_id)
        u_id = u_payload['user_id']

        # Verify quota is initially 0
        user_before = auth.get_user_by_id(u_id)
        self.assertIsNotNone(user_before)
        self.assertEqual(user_before['quota_used_bytes'], 0)

        # Set auth.add_file to raise (triggering exception path)
        mock_add_file.side_effect = Exception("SQLite Database Failure")

        sock_ping = SocketMock(b"OK PONG\n")
        sock_upload = SocketMock(b"READY\nOK FILE_SAVED comp_del_fail_file.txt\n")
        # Compensating DELETE fails (connection refused)
        sock_delete = SocketMock(b"", raise_on_connect=ConnectionRefusedError("Offline"))
        mock_socket_class.side_effect = [sock_ping, sock_upload, sock_delete]

        data = {
            'file': (tempfile.SpooledTemporaryFile(), 'comp_del_fail_file.txt')
        }
        data['file'][0].write(b"some content")
        data['file'][0].seek(0)

        response = self.client.post('/upload', data=data, content_type='multipart/form-data')
        self.assertEqual(response.status_code, 500)

        # Quota must NOT be rolled back — file still exists on disk
        user_after = auth.get_user_by_id(u_id)
        self.assertIsNotNone(user_after)
        self.assertEqual(user_after['quota_used_bytes'], 12)  # len(b"some content")

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

    @patch('app.socket.socket')
    def test_upload_malformed_filename(self, mock_socket_class):
        self.client.post('/register', data={
            'username': 'mal_up',
            'email': 'mal_up@example.com',
            'password': 'password123',
            'confirm_password': 'password123'
        })
        self.client.post('/login', data={
            'username': 'mal_up',
            'password': 'password123'
        })
        
        sock_ping = SocketMock(b"OK PONG\n")
        mock_socket_class.side_effect = [sock_ping]
        
        data = {
            'file': (tempfile.SpooledTemporaryFile(), '../..')
        }
        data['file'][0].write(b"content")
        data['file'][0].seek(0)
        
        response = self.client.post('/upload', data=data, content_type='multipart/form-data')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['status'], 'error')
        self.assertEqual(response.get_json()['message'], 'Invalid filename')
        # Only ping socket should be created, no upload socket
        self.assertEqual(mock_socket_class.call_count, 1)

    @patch('app.socket.socket')
    def test_rename_malformed_filename(self, mock_socket_class):
        self.client.post('/register', data={
            'username': 'mal_ren',
            'email': 'mal_ren@example.com',
            'password': 'password123',
            'confirm_password': 'password123'
        })
        self.client.post('/login', data={
            'username': 'mal_ren',
            'password': 'password123'
        })
        
        user_id = auth.login_user('mal_ren', 'password123')
        u_payload = auth.decode_token(user_id)
        u_id = u_payload['user_id']
        auth.add_file('existing.txt', 'existing.txt', 'document', 10, u_id)
        
        sock_ping = SocketMock(b"OK PONG\n")
        mock_socket_class.side_effect = [sock_ping]
        
        response = self.client.post('/rename/existing.txt', data={'new_name': '..'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['status'], 'error')
        self.assertEqual(response.get_json()['message'], 'Invalid filename')
        # Only ping socket should be created, no rename socket
        self.assertEqual(mock_socket_class.call_count, 1)

    @patch('app.socket.socket')
    def test_upload_tcp_invalid_filename_mapping(self, mock_socket_class):
        self.client.post('/register', data={
            'username': 'tcp_invalid',
            'email': 'tcp_invalid@example.com',
            'password': 'password123',
            'confirm_password': 'password123'
        })
        self.client.post('/login', data={
            'username': 'tcp_invalid',
            'password': 'password123'
        })
        
        sock_ping = SocketMock(b"OK PONG\n")
        sock_upload = SocketMock(b"ERROR INVALID_FILENAME\n")
        mock_socket_class.side_effect = [sock_ping, sock_upload]
        
        data = {
            'file': (tempfile.SpooledTemporaryFile(), 'test_invalid.txt')
        }
        data['file'][0].write(b"content")
        data['file'][0].seek(0)
        
        response = self.client.post('/upload', data=data, content_type='multipart/form-data')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['status'], 'error')
        self.assertEqual(response.get_json()['message'], 'Invalid filename')

    @patch('app.socket.socket')
    def test_rename_tcp_invalid_filename_mapping(self, mock_socket_class):
        self.client.post('/register', data={
            'username': 'tcp_ren_invalid',
            'email': 'tcp_ren_invalid@example.com',
            'password': 'password123',
            'confirm_password': 'password123'
        })
        self.client.post('/login', data={
            'username': 'tcp_ren_invalid',
            'password': 'password123'
        })
        
        user_id = auth.login_user('tcp_ren_invalid', 'password123')
        u_payload = auth.decode_token(user_id)
        u_id = u_payload['user_id']
        auth.add_file('existing_tcp.txt', 'existing_tcp.txt', 'document', 10, u_id)
        
        sock_ping = SocketMock(b"OK PONG\n")
        sock_rename = SocketMock(b"ERROR INVALID_FILENAME\n")
        mock_socket_class.side_effect = [sock_ping, sock_rename]
        
        response = self.client.post('/rename/existing_tcp.txt', data={'new_name': 'new_invalid.txt'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['status'], 'error')
        self.assertEqual(response.get_json()['message'], 'Invalid filename')

    @patch('app.socket.socket')
    def test_upload_tcp_invalid_command_mapping(self, mock_socket_class):
        self.client.post('/register', data={
            'username': 'tcp_cmd',
            'email': 'tcp_cmd@example.com',
            'password': 'password123',
            'confirm_password': 'password123'
        })
        self.client.post('/login', data={
            'username': 'tcp_cmd',
            'password': 'password123'
        })
        
        sock_ping = SocketMock(b"OK PONG\n")
        sock_upload = SocketMock(b"ERROR INVALID_COMMAND\n")
        mock_socket_class.side_effect = [sock_ping, sock_upload]
        
        data = {
            'file': (tempfile.SpooledTemporaryFile(), 'test_cmd.txt')
        }
        data['file'][0].write(b"content")
        data['file'][0].seek(0)
        
        response = self.client.post('/upload', data=data, content_type='multipart/form-data')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['status'], 'error')
        self.assertEqual(response.get_json()['message'], 'Invalid command')

    @patch('app.socket.socket')
    def test_download_success_delayed_body(self, mock_socket_class):
        self.client.post('/register', data={
            'username': 'dl_delayed',
            'email': 'dl_delayed@example.com',
            'password': 'password123',
            'confirm_password': 'password123'
        })
        self.client.post('/login', data={
            'username': 'dl_delayed',
            'password': 'password123'
        })
        
        user_id = auth.login_user('dl_delayed', 'password123')
        u_payload = auth.decode_token(user_id)
        u_id = u_payload['user_id']
        
        auth.add_file('delayed_file.txt', 'orig_delayed.txt', 'document', 11, u_id)
        
        sock_ping = SocketMock(b"OK PONG\n")
        sock_download = DelayedBodySocketMock(b"OK 11\nhello world")
        mock_socket_class.side_effect = [sock_ping, sock_download]
        
        # Reset the global failover state
        import app as app_module
        app_module.PRIMARY_DOWN = False
        
        response = self.client.get('/download/delayed_file.txt')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"hello world")
        self.assertEqual(response.headers['Content-Length'], '11')

    @patch('app.socket.socket')
    @patch('app.auth.delete_file_and_decrement_quota')
    def test_delete_db_failure_reconciliation_success(self, mock_delete_helper, mock_socket_class):
        self.client.post('/register', data={
            'username': 'del_recon',
            'email': 'del_recon@example.com',
            'password': 'password123',
            'confirm_password': 'password123'
        })
        self.client.post('/login', data={
            'username': 'del_recon',
            'password': 'password123'
        })
        
        user_id = auth.login_user('del_recon', 'password123')
        u_payload = auth.decode_token(user_id)
        u_id = u_payload['user_id']
        auth.add_file('del_recon.txt', 'del_recon.txt', 'document', 20, u_id)
        auth.update_quota(u_id, 20)
        
        # Force the main helper to raise an exception
        mock_delete_helper.side_effect = Exception("Simulated main DB failure")
        
        sock_ping = SocketMock(b"OK PONG\n")
        sock_delete = SocketMock(b"OK FILE_DELETED\n")
        mock_socket_class.side_effect = [sock_ping, sock_delete]
        
        response = self.client.post('/delete/del_recon.txt')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['status'], 'success')
        
        # Verify the file is still deleted locally (due to reconciliation)
        self.assertIsNone(auth.get_file_by_name('del_recon.txt'))
        user = auth.get_user_by_id(u_id)
        self.assertEqual(user['quota_used_bytes'], 0)

    @patch('app.socket.socket')
    @patch('app.auth.delete_file_and_decrement_quota')
    def test_delete_db_failure_reconciliation_failure(self, mock_delete_helper, mock_socket_class):
        self.client.post('/register', data={
            'username': 'del_recon_fail',
            'email': 'del_recon_fail@example.com',
            'password': 'password123',
            'confirm_password': 'password123'
        })
        self.client.post('/login', data={
            'username': 'del_recon_fail',
            'password': 'password123'
        })
        
        user_id = auth.login_user('del_recon_fail', 'password123')
        u_payload = auth.decode_token(user_id)
        u_id = u_payload['user_id']
        auth.add_file('del_recon_fail.txt', 'del_recon_fail.txt', 'document', 20, u_id)
        auth.update_quota(u_id, 20)
        
        fail_connection = False
        def fail_delete_helper(*args, **kwargs):
            nonlocal fail_connection
            fail_connection = True
            raise Exception("Simulated main DB failure")
            
        mock_delete_helper.side_effect = fail_delete_helper
        
        import database
        original_get_connection = database.get_connection
        def mock_get_conn_fn():
            if fail_connection:
                raise Exception("Simulated connection failure during reconciliation")
            return original_get_connection()
        
        sock_ping = SocketMock(b"OK PONG\n")
        sock_delete = SocketMock(b"OK FILE_DELETED\n")
        mock_socket_class.side_effect = [sock_ping, sock_delete]
        
        with patch('database.get_connection', side_effect=mock_get_conn_fn):
            response = self.client.post('/delete/del_recon_fail.txt')
            
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json()['status'], 'error')

    @patch('app.socket.socket')
    @patch('app.auth.rename_file')
    def test_rename_db_failure_compensation(self, mock_rename_helper, mock_socket_class):
        self.client.post('/register', data={
            'username': 'ren_comp',
            'email': 'ren_comp@example.com',
            'password': 'password123',
            'confirm_password': 'password123'
        })
        self.client.post('/login', data={
            'username': 'ren_comp',
            'password': 'password123'
        })
        
        user_id = auth.login_user('ren_comp', 'password123')
        u_payload = auth.decode_token(user_id)
        u_id = u_payload['user_id']
        auth.add_file('ren_old.txt', 'ren_old.txt', 'document', 10, u_id)
        
        mock_rename_helper.side_effect = Exception("Simulated rename DB failure")
        
        sock_ping = SocketMock(b"OK PONG\n")
        sock_rename = SocketMock(b"OK FILE_RENAMED ren_new.txt\n")
        sock_compensate = SocketMock(b"OK FILE_RENAMED ren_old.txt\n")
        mock_socket_class.side_effect = [sock_ping, sock_rename, sock_compensate]
        
        response = self.client.post('/rename/ren_old.txt', data={'new_name': 'ren_new.txt'})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json()['status'], 'error')
        self.assertEqual(response.get_json()['message'], 'Rename failed')
        
        # Verify compensating RENAME was sent
        self.assertIn(b"RENAME ren_new.txt ren_old.txt", sock_compensate.sent_data)

class TestQuotaReservationIntegration:
    """
    HTTP-level tests verifying that the upload route correctly uses
    reserve_quota / release_quota (atomic) rather than a non-atomic
    check → update sequence.
    """

    def setUp_user(self, client, username, email):
        """Helper: register + login a user, return their auth user_id."""
        client.post('/register', data={
            'username': username,
            'email': email,
            'password': 'password123',
            'confirm_password': 'password123'
        })
        client.post('/login', data={
            'username': username,
            'password': 'password123'
        })
        token = auth.login_user(username, 'password123')
        payload = auth.decode_token(token)
        return payload['user_id']


class TestQuotaReservation(unittest.TestCase):
    """Integration: reserve_quota path in /upload.\""""

    def setUp(self):
        app.config['TESTING'] = True
        app.config['SECRET_KEY'] = 'test-secret-key'
        self.client = app.test_client()
        init_db()
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

    def _register_login(self, username, email='test@example.com'):
        self.client.post('/register', data={
            'username': username,
            'email': email,
            'password': 'password123',
            'confirm_password': 'password123'
        })
        self.client.post('/login', data={
            'username': username,
            'password': 'password123'
        })
        token = auth.login_user(username, 'password123')
        return auth.decode_token(token)['user_id']

    @patch('app.socket.socket')
    def test_reserve_quota_increments_on_successful_upload(self, mock_socket_class):
        """quota_used_bytes should equal file_size after a successful upload."""
        u_id = self._register_login('res_ok', 'res_ok@example.com')

        sock_ping = SocketMock(b"OK PONG\n")
        sock_upload = SocketMock(b"READY\nOK FILE_SAVED reserved_ok.txt\n")
        mock_socket_class.side_effect = [sock_ping, sock_upload]

        data = {'file': (tempfile.SpooledTemporaryFile(), 'reserved_ok.txt')}
        data['file'][0].write(b"X" * 500)
        data['file'][0].seek(0)

        response = self.client.post('/upload', data=data, content_type='multipart/form-data')
        self.assertEqual(response.status_code, 200)

        user = auth.get_user_by_id(u_id)
        self.assertIsNotNone(user)
        self.assertEqual(user['quota_used_bytes'], 500)

    @patch('app.socket.socket')
    def test_quota_exhaustion_returns_403_no_db_change(self, mock_socket_class):
        """When reserve_quota fails, upload returns 403 and quota is unchanged."""
        u_id = self._register_login('res_full', 'res_full@example.com')
        # Fill quota to 1 byte below limit (leaving only 1 byte free).
        auth.update_quota(u_id, 52_428_800 - 1)

        sock_ping = SocketMock(b"OK PONG\n")
        mock_socket_class.side_effect = [sock_ping]

        data = {'file': (tempfile.SpooledTemporaryFile(), 'overflow.txt')}
        data['file'][0].write(b"XX")   # 2 bytes — 1 more than the remaining quota
        data['file'][0].seek(0)

        response = self.client.post('/upload', data=data, content_type='multipart/form-data')
        self.assertEqual(response.status_code, 403)
        self.assertIn('quota exceeded', response.get_json()['message'].lower())

        # quota_used_bytes must be unchanged — no reservation was made.
        user = auth.get_user_by_id(u_id)
        self.assertIsNotNone(user)
        self.assertEqual(user['quota_used_bytes'], 52_428_800 - 1)

    @patch('app.socket.socket')
    @patch('auth.add_file')
    def test_quota_rolled_back_when_db_metadata_write_fails(self, mock_add_file, mock_socket_class):
        """
        reserve_quota succeeds, TCP saves the file, then auth.add_file raises →
        the route must call release_quota so quota_used_bytes returns to 0.
        """
        u_id = self._register_login('res_rollback', 'res_rb@example.com')

        mock_add_file.side_effect = Exception("Simulated DB failure")

        sock_ping = SocketMock(b"OK PONG\n")
        sock_upload = SocketMock(b"READY\nOK FILE_SAVED rb_file.txt\n")
        sock_delete = SocketMock(b"OK FILE_DELETED\n")
        mock_socket_class.side_effect = [sock_ping, sock_upload, sock_delete]

        data = {'file': (tempfile.SpooledTemporaryFile(), 'rb_file.txt')}
        data['file'][0].write(b"rollback me")
        data['file'][0].seek(0)

        response = self.client.post('/upload', data=data, content_type='multipart/form-data')
        self.assertEqual(response.status_code, 500)

        # Quota must be fully rolled back.
        user = auth.get_user_by_id(u_id)
        self.assertIsNotNone(user)
        self.assertEqual(user['quota_used_bytes'], 0)

        # Compensating DELETE must have been sent to the TCP server.
        self.assertIn(b"DELETE rb_file.txt", sock_delete.sent_data)

    @patch('app.socket.socket')
    def test_concurrent_uploads_only_one_fits_remaining_quota(self, mock_socket_class):
        """
        Two simultaneous /upload requests where only one fits in the remaining
        quota.  Exactly one must get HTTP 200, the other HTTP 403.
        """
        import threading

        u_id = self._register_login('res_concurrent', 'res_con@example.com')
        # Leave 8 MB free; each upload tries to claim 5 MB.
        auth.update_quota(u_id, 52_428_800 - 8_000_000)

        # Each request needs its own ping + upload sockets (only 1 will reach upload).
        sock_ping1 = SocketMock(b"OK PONG\n")
        sock_ping2 = SocketMock(b"OK PONG\n")
        sock_upload = SocketMock(b"READY\nOK FILE_SAVED concurrent.txt\n")
        mock_socket_class.side_effect = [sock_ping1, sock_upload, sock_ping2]

        statuses: list[int] = []
        lock = threading.Lock()

        def do_upload():
            data = {'file': (tempfile.SpooledTemporaryFile(), 'concurrent.txt')}
            data['file'][0].write(b"A" * 5_000_000)
            data['file'][0].seek(0)
            resp = self.client.post('/upload', data=data, content_type='multipart/form-data')
            with lock:
                statuses.append(resp.status_code)

        t1 = threading.Thread(target=do_upload)
        t2 = threading.Thread(target=do_upload)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertIn(200, statuses)
        self.assertIn(403, statuses)

    def test_sliding_window_active_vs_idle(self):
        # 1. Login user to get initial session token
        self._register_login('sliding_user', 'sliding@example.com')
        with self.client.session_transaction() as sess:
            orig_token = sess.get('token')
        
        self.assertIsNotNone(orig_token)
        orig_payload = auth.decode_token(orig_token)
        
        # 2. Simulate activity: request dashboard
        # Make a mock socket response since dashboard calls list/ping
        with patch('app.socket.socket') as mock_sock:
            mock_sock.side_effect = [SocketMock(b"OK PONG\n"), SocketMock(b"OK []\n")]
            response = self.client.get('/dashboard')
            self.assertEqual(response.status_code, 200)
        
        # 3. Check that session token was refreshed/updated
        with self.client.session_transaction() as sess:
            refreshed_token = sess.get('token')
            
        self.assertIsNotNone(refreshed_token)
        self.assertNotEqual(orig_token, refreshed_token)
        
        refreshed_payload = auth.decode_token(refreshed_token)
        # Expiry timestamp should have advanced
        self.assertGreater(refreshed_payload['exp'], orig_payload['exp'])

        # 4. Idle/Expired token: simulate expired token by setting exp to past
        import jwt as _jwt
        from datetime import datetime, timezone, timedelta
        import config
        
        expired_payload = {
            "user_id": orig_payload['user_id'],
            "username": "sliding_user",
            "exp": datetime.now(timezone.utc) - timedelta(minutes=5),
            "iat": datetime.now(timezone.utc) - timedelta(minutes=31),
        }
        expired_token = _jwt.encode(
            expired_payload, config.JWT_SECRET_KEY, algorithm=config.JWT_ALGORITHM
        )
        
        with self.client.session_transaction() as sess:
            sess['token'] = expired_token
            
        # Accessing dashboard should fail auth, clear session, and redirect to /login
        with patch('app.socket.socket') as mock_sock:
            mock_sock.side_effect = [SocketMock(b"OK PONG\n")]
            response = self.client.get('/dashboard')
            self.assertEqual(response.status_code, 302)
            self.assertIn('/login', response.headers.get('Location', ''))
            
        with self.client.session_transaction() as sess:
            self.assertNotIn('token', sess)

    def test_auth_validation_status_codes(self):
        # Missing login fields
        resp = self.client.post('/login', data={'username': '', 'password': ''})
        self.assertEqual(resp.status_code, 400)
        
        # Invalid credentials
        resp = self.client.post('/login', data={'username': 'nonexistent', 'password': 'pw'})
        self.assertEqual(resp.status_code, 200) # aligned with documented login contract (200 OK)
        
        # Missing registration fields
        resp = self.client.post('/register', data={'username': '', 'email': '', 'password': ''})
        self.assertEqual(resp.status_code, 400)
        
        # Passwords mismatch
        resp = self.client.post('/register', data={
            'username': 'mismatch', 'email': 'mis@ex.com', 'password': 'p1', 'confirm_password': 'p2'
        })
        self.assertEqual(resp.status_code, 400)
        
        # Duplicate registration
        self.client.post('/register', data={
            'username': 'dup', 'email': 'dup@ex.com', 'password': 'p', 'confirm_password': 'p'
        })
        # Try registering same username again
        resp = self.client.post('/register', data={
            'username': 'dup', 'email': 'dup2@ex.com', 'password': 'p', 'confirm_password': 'p'
        })
        self.assertEqual(resp.status_code, 409)
        # Try registering same email again
        resp = self.client.post('/register', data={
            'username': 'dup2', 'email': 'dup@ex.com', 'password': 'p', 'confirm_password': 'p'
        })
        self.assertEqual(resp.status_code, 409)

    @patch('auth.get_user_by_id')
    def test_load_user_lookup_error_propagation(self, mock_get_user):
        # Set mock to raise a database exception
        mock_get_user.side_effect = sqlite3.OperationalError("Database locked or connection failed")
        
        # Create a valid session token
        u_id = self._register_login('infra_fail_user', 'infra_fail@ex.com')
        
        # Attempt to access dashboard — the DB failure must NOT be caught/masked by load_user.
        # It should propagate (raising the error to Flask/unittest runner, causing 500 or error).
        with patch('app.socket.socket') as mock_sock:
            mock_sock.side_effect = [SocketMock(b"OK PONG\n")]
            with self.assertRaises(sqlite3.OperationalError):
                self.client.get('/dashboard')

    @patch('app.ping_server')
    def test_auth_routes_responsive_during_outage(self, mock_ping):
        # When primary server is down, ping_server returns False (offline)
        mock_ping.return_value = False
        
        # Access login page - should load successfully (200 OK) without hanging
        resp = self.client.get('/login')
        self.assertEqual(resp.status_code, 200)
        
        # Access register page - should load successfully
        resp = self.client.get('/register')
        self.assertEqual(resp.status_code, 200)
        
        # Logging in should function and succeed even if primary server is offline
        # (write operations are disabled but login/session itself works)
        self.client.post('/register', data={
            'username': 'outage_user',
            'email': 'outage@ex.com',
            'password': 'password123',
            'confirm_password': 'password123'
        })
        
        resp = self.client.post('/login', data={
            'username': 'outage_user',
            'password': 'password123'
        })
        # Successful login redirects to dashboard (302)
        self.assertEqual(resp.status_code, 302)

    def test_session_security_configuration(self):
        # Verify secret_key is not the unsafe default development key
        self.assertNotEqual(app.secret_key, "super-secret-key-change-in-prod")
        
        # Verify session security settings
        self.assertTrue(app.config.get('SESSION_COOKIE_HTTPONLY'))
        self.assertEqual(app.config.get('SESSION_COOKIE_SAMESITE'), 'Lax')
        # Secure flag is boolean (False by default in test env unless configured)
        self.assertIn(app.config.get('SESSION_COOKIE_SECURE'), (True, False))

    @patch('app.socket.socket')
    def test_download_auth_validation(self, mock_socket_class):
        # Register and login a user
        self.client.post('/register', data={
            'username': 'auth_fail_downloader',
            'email': 'afd@example.com',
            'password': 'password123',
            'confirm_password': 'password123'
        })
        self.client.post('/login', data={
            'username': 'auth_fail_downloader',
            'password': 'password123'
        })
        
        user_id = auth.login_user('auth_fail_downloader', 'password123')
        u_payload = auth.decode_token(user_id)
        u_id = u_payload['user_id']
        auth.add_file('auth_fail.txt', 'auth_fail.txt', 'document', 10, u_id)
        
        # 1. PING succeeds
        sock_ping = SocketMock(b"OK PONG\n")
        # 2. Primary download connection fails auth
        sock_primary_fail = SocketMock(b"ERROR UNAUTHORIZED\n", skip_auth_prefix=True)
        # 3. Replica download connection also fails auth
        sock_replica_fail = SocketMock(b"ERROR UNAUTHORIZED\n", skip_auth_prefix=True)
        
        mock_socket_class.side_effect = [sock_ping, sock_primary_fail, sock_replica_fail]
        
        response = self.client.get('/download/auth_fail.txt')
        self.assertEqual(response.status_code, 500)
        self.assertIn(b"Failed to connect to file server.", response.data)
        
        from config import TCP_CLIENT_SECRET
        expected_auth_cmd = f"AUTH {TCP_CLIENT_SECRET}\n"
        self.assertTrue(sock_primary_fail.sent_data.startswith(expected_auth_cmd.encode('utf-8')))


if __name__ == '__main__':
    unittest.main()

