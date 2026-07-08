"""
test_t3.py — Tests for the T3 epic requirements: Shareable Links & Visibility Control.

Run with: python -m pytest test_t3.py -v
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

from database import init_db, get_connection, get_file_by_name, get_file_shares
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


def test_visibility_and_share_link_management():
    """Verify owner can manage visibility, add/remove users, and reuse share token."""
    client1 = app.test_client()
    client2 = app.test_client()
    
    # Register Alice and Bob
    with client1:
        register_and_login(client1, "alice", "alice@example.com", "pass123")
    with client2:
        register_and_login(client2, "bob", "bob@example.com", "pass123")
        
    with client1:
        # Add a file for Alice
        auth.add_file("alice_file.txt", "Alice File", "text", 100, 1)
        
        # 1. Check default sharing status
        res = client1.get('/share/alice_file.txt')
        assert res.status_code == 200
        data = res.get_json()
        assert data['visibility'] == 'public'
        assert data['share_token'] is None
        assert data['shared_users'] == []
        
        # 2. Update to public visibility (creates a token)
        res = client1.post('/share/alice_file.txt', data={
            'visibility': 'public'
        })
        assert res.status_code == 200
        data = res.get_json()
        assert data['visibility'] == 'public'
        token1 = data['share_token']
        assert token1 is not None
        
        # 3. Update again (should reuse the same token)
        res = client1.post('/share/alice_file.txt', data={
            'visibility': 'shared',
            'usernames': 'bob'
        })
        assert res.status_code == 200
        data = res.get_json()
        assert data['visibility'] == 'shared'
        assert data['share_token'] == token1
        assert 'bob' in data['shared_users']
        
        # Verify in DB and file shares list
        shares = get_file_shares(get_file_by_name("alice_file.txt")['id'])
        assert 'bob' in shares
        
        # 4. Remove Bob from share list
        res = client1.post('/share/alice_file.txt', data={
            'visibility': 'shared',
            'usernames': ''
        })
        assert res.status_code == 200
        data = res.get_json()
        assert data['shared_users'] == []
        shares = get_file_shares(get_file_by_name("alice_file.txt")['id'])
        assert 'bob' not in shares


def test_sharing_access_rejection_for_non_owner():
    """Verify non-owners cannot change sharing settings of a file."""
    client1 = app.test_client()
    client2 = app.test_client()
    
    with client1:
        register_and_login(client1, "alice", "alice@example.com", "pass123")
        auth.add_file("alice_file.txt", "Alice File", "text", 100, 1)
        
    with client2:
        register_and_login(client2, "bob", "bob@example.com", "pass123")
        # Try to modify Alice's file sharing settings
        res = client2.post('/share/alice_file.txt', data={
            'visibility': 'public'
        })
        assert res.status_code == 403
        assert "only share files you own" in res.get_json()['message']


def test_public_unauthenticated_file_access():
    """Verify GET /shared/<token> serves files when public and rejects appropriately."""
    client = app.test_client()
    
    # Create file for owner
    init_db()
    u1 = auth.register_user("alice", "alice@example.com", "pass123")
    auth.add_file("alice_file.txt", "Alice File.txt", "text", 12, u1['id'], visibility='public')
    auth.add_file_share("alice_file.txt", "alice", u1['id']) # triggers token generation
    
    file_record = get_file_by_name("alice_file.txt")
    token = file_record['share_token']
    assert token is not None
    
    # 1. Unauthenticated page access (no download parameter)
    res = client.get(f'/shared/{token}')
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "Alice File.txt" in html
    assert "alice" in html
    
    # 2. Unauthenticated stream download
    with patch('app.connect_and_authenticate') as mock_conn:
        import socket
        mock_socket = mock_conn.return_value
        # Mock responses from TCP server: "OK 12\n" + data
        mock_socket.recv.side_effect = [b"OK 12\n", b"file content"]
        
        res = client.get(f'/shared/{token}?download=true')
        assert res.status_code == 200
        assert res.headers['Content-Disposition'] == 'attachment; filename="Alice_File.txt"'
        assert res.get_data() == b"file content"
        
    # 3. Access when visibility is private (returns 403)
    auth.set_file_visibility("alice_file.txt", "private", u1['id'])
    res = client.get(f'/shared/{token}')
    assert res.status_code == 403
    
    # 4. Access with unknown token (returns 404)
    res = client.get('/shared/unknown-token-1234')
    assert res.status_code == 404


def test_download_visibility_restrictions():
    """Verify download endpoint enforces visibility and share settings for logged-in users."""
    client1 = app.test_client()
    client2 = app.test_client()
    
    # Register Alice and Bob
    with client1:
        register_and_login(client1, "alice", "alice@example.com", "pass123")
        auth.add_file("priv.txt", "Private", "text", 10, 1, visibility='private')
        auth.add_file("shared.txt", "Shared", "text", 10, 1, visibility='shared')
        
    with client2:
        register_and_login(client2, "bob", "bob@example.com", "pass123")
        
        # Bob tries to download Alice's private file (403)
        res = client2.get('/download/priv.txt')
        assert res.status_code == 403
        
        # Bob tries to download Alice's shared file before being added (403)
        res = client2.get('/download/shared.txt')
        assert res.status_code == 403
        
    with client1:
        # Add Bob to share list
        auth.add_file_share("shared.txt", "bob", 1)
        
    with client2:
        # Bob tries to download Alice's shared file after being added (200)
        with patch('app.connect_and_authenticate') as mock_conn:
            mock_socket = mock_conn.return_value
            mock_socket.recv.side_effect = [b"OK 10\n", b"file bytes"]
            res = client2.get('/download/shared.txt')
            assert res.status_code == 200
            assert res.get_data() == b"file bytes"


def test_failed_sharing_does_not_mutate_state():
    """Verify that a failed share request (e.g., due to an invalid username) does not change visibility or share token."""
    client1 = app.test_client()
    
    with client1:
        register_and_login(client1, "alice", "alice@example.com", "pass123")
        # Add a file for Alice with public visibility
        auth.add_file("alice_file.txt", "Alice File", "text", 100, 1, visibility='public')
        
        # Verify initial state
        file_rec_before = get_file_by_name("alice_file.txt")
        assert file_rec_before['visibility'] == 'public'
        assert file_rec_before['share_token'] is None
        
        # Attempt to share with a non-existent user and change visibility to 'shared'
        res = client1.post('/share/alice_file.txt', data={
            'visibility': 'shared',
            'usernames': 'nonexistentuser'
        })
        assert res.status_code == 400
        assert "does not exist" in res.get_json()['message']
        
        # Verify state is completely unchanged
        file_rec_after = get_file_by_name("alice_file.txt")
        assert file_rec_after['visibility'] == 'public'
        assert file_rec_after['share_token'] is None
        
        # Also check that there are no shares created
        shares = get_file_shares(file_rec_after['id'])
        assert shares == []


def test_visibility_mode_switch_preserves_shares():
    """Verify that switching visibility to public/private preserves existing shares, and only 'shared' modifies them."""
    client1 = app.test_client()
    
    with client1:
        register_and_login(client1, "alice", "alice@example.com", "pass123")
        # Also register Bob so he exists
        client2 = app.test_client()
        with client2:
            register_and_login(client2, "bob", "bob@example.com", "pass123")
            
        # Alice adds a file
        auth.add_file("alice_file.txt", "Alice File", "text", 100, 1)
        
        # 1. Alice shares file with Bob (visibility='shared')
        res = client1.post('/share/alice_file.txt', data={
            'visibility': 'shared',
            'usernames': 'bob'
        })
        assert res.status_code == 200
        shares = get_file_shares(get_file_by_name("alice_file.txt")['id'])
        assert 'bob' in shares
        
        # 2. Alice changes visibility to 'public' without specifying usernames
        res = client1.post('/share/alice_file.txt', data={
            'visibility': 'public'
        })
        assert res.status_code == 200
        # Bob should still be in get_file_shares because we preserve existing shares!
        shares = get_file_shares(get_file_by_name("alice_file.txt")['id'])
        assert 'bob' in shares
        
        # 3. Alice changes visibility to 'private' (without specifying usernames)
        res = client1.post('/share/alice_file.txt', data={
            'visibility': 'private'
        })
        assert res.status_code == 200
        # Bob should still be in get_file_shares because we preserve existing shares!
        shares = get_file_shares(get_file_by_name("alice_file.txt")['id'])
        assert 'bob' in shares
