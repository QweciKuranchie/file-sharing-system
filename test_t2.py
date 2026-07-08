"""
test_t2.py — Tests for the T2 epic requirements: Folder Creation & Navigation.

Run with: python -m pytest test_t2.py -v
"""

import os
import sys
import tempfile
import pytest
from unittest.mock import patch

# Override database path in config BEFORE importing app or database
import config
_TEST_DB_FD, _TEST_DB_PATH = tempfile.mkstemp(suffix=".db")
os.close(_TEST_DB_FD)
config.DATABASE_PATH = _TEST_DB_PATH
config.JWT_SECRET_KEY = "test-secret-key-do-not-use-in-production"

from database import init_db, get_connection, get_file_by_name, get_folder_by_id
from app import app
import auth


@pytest.fixture(autouse=True)
def setup_and_teardown():
    """Ensure a clean database for each test."""
    # Setup
    init_db()
    
    # Configure app for testing
    app.config['TESTING'] = True
    app.config['SECRET_KEY'] = 'test-secret-key'
    
    yield
    
    # Teardown
    try:
        if os.path.exists(_TEST_DB_PATH):
            os.remove(_TEST_DB_PATH)
        for suffix in ["-wal", "-shm"]:
            wal_path = _TEST_DB_PATH + suffix
            if os.path.exists(wal_path):
                os.remove(wal_path)
    except OSError:
        pass


def register_and_login(client, username, email, password):
    """Helper to register and login a user."""
    client.post('/register', data={
        'username': username,
        'email': email,
        'password': password,
        'confirm_password': password
    })
    client.post('/login', data={
        'username': username,
        'password': password
    })


def test_create_folder_route():
    """Verify folder creation works via POST /folders, handles nesting and constraints."""
    client = app.test_client()
    with client:
        register_and_login(client, "alice", "alice@example.com", "pass123")
        
        # 1. Create a top-level folder
        response = client.post('/folders', data={
            'name': 'Top Folder'
        })
        assert response.status_code == 200
        data = response.get_json()
        assert data['status'] == 'success'
        f1_id = data['folder_id']
        assert f1_id > 0
        
        # Verify in DB
        folder1 = get_folder_by_id(f1_id)
        assert folder1 is not None
        assert folder1['name'] == 'Top Folder'
        assert folder1['parent_folder_id'] is None
        
        # 2. Create a nested folder
        response = client.post('/folders', data={
            'name': 'Sub Folder',
            'parent_folder_id': str(f1_id)
        })
        assert response.status_code == 200
        data = response.get_json()
        f2_id = data['folder_id']
        
        # Verify in DB
        folder2 = get_folder_by_id(f2_id)
        assert folder2 is not None
        assert folder2['name'] == 'Sub Folder'
        assert folder2['parent_folder_id'] == f1_id
        
        # 3. Create folder with invalid empty name
        response = client.post('/folders', data={
            'name': '   '
        })
        assert response.status_code == 400
        assert response.get_json()['status'] == 'error'


def test_delete_folder_route_and_ownership():
    """Verify delete folder route removes folder and enforces ownership checks."""
    client1 = app.test_client()
    client2 = app.test_client()
    
    # Register Alice and Bob
    with client1:
        register_and_login(client1, "alice", "alice@example.com", "pass123")
        response = client1.post('/folders', data={'name': 'Alice Folder'})
        f_id = response.get_json()['folder_id']
        
    with client2:
        register_and_login(client2, "bob", "bob@example.com", "pass123")
        # Try to delete Alice's folder as Bob
        response = client2.post(f'/folders/{f_id}/delete')
        assert response.status_code == 403
        
    with client1:
        # Delete Alice's folder as Alice
        response = client1.post(f'/folders/{f_id}/delete')
        assert response.status_code == 200
        assert get_folder_by_id(f_id) is None


def test_dashboard_scoped_view_and_navigation():
    """Verify files and subfolders are scoped correctly on the dashboard."""
    client = app.test_client()
    with client:
        register_and_login(client, "alice", "alice@example.com", "pass123")
        user = auth.get_user_by_id(1) # Alice's ID is 1 (first registered)
        
        # Create Folder 1 and Folder 2
        f1_id = auth.create_folder("Folder 1", 1)
        f2_id = auth.create_folder("Folder 2", 1)
        
        # Add files to different scopes
        auth.add_file("file_root.txt", "File Root", "text", 100, 1, folder_id=None)
        auth.add_file("file_f1.txt", "File F1", "text", 100, 1, folder_id=f1_id)
        
        # 1. Access root dashboard (folder_id=None/omitted)
        response = client.get('/dashboard')
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        # Should see root files and top-level folders
        assert "File Root" in html
        assert "Folder 1" in html
        assert "Folder 2" in html
        # Should NOT see nested files
        assert "File F1" not in html
        
        # 2. Access scoped dashboard (folder_id=f1_id)
        response = client.get(f'/dashboard?folder_id={f1_id}')
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        # Should see nested file inside Folder 1
        assert "File F1" in html
        # Should NOT see root files or folder rows
        assert "File Root" not in html
        
        # 3. Access invalid folder_id
        response = client.get('/dashboard?folder_id=99999', follow_redirects=True)
        # Redirects to root dashboard
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert "Folder not found." in html


def test_upload_file_in_folder():
    """Verify file upload inside a folder associates correctly."""
    client = app.test_client()
    with client:
        register_and_login(client, "alice", "alice@example.com", "pass123")
        f_id = auth.create_folder("Upload Dest", 1)
        
        import io
        file_data = (io.BytesIO(b"file content"), 'test.txt')
        
        # Mock TCP command
        with patch('app.send_tcp_command', return_value="OK FILE_SAVED 12 test.txt"):
            response = client.post('/upload', data={
                'file': file_data,
                'folder_id': str(f_id)
            })
            assert response.status_code == 200
            
        # Verify file record in database has folder_id
        f_record = get_file_by_name("test.txt")
        assert f_record is not None
        assert f_record['folder_id'] == f_id


def test_delete_folder_moves_files_to_root():
    """Verify files move to root when their folder is deleted."""
    client = app.test_client()
    with client:
        register_and_login(client, "alice", "alice@example.com", "pass123")
        f_id = auth.create_folder("Temp Folder", 1)
        
        auth.add_file("nested.txt", "Nested", "text", 50, 1, folder_id=f_id)
        
        # Delete folder
        response = client.post(f'/folders/{f_id}/delete')
        assert response.status_code == 200
        
        # File should still exist but its folder_id should be NULL
        f_record = get_file_by_name("nested.txt")
        assert f_record is not None
        assert f_record['folder_id'] is None
