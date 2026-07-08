"""
test_t1.py — Tests for the T1 epic requirements: Schema Migration for Folders & Sharing.

Run with: python -m pytest test_t1.py -v
"""

import os
import sys
import sqlite3
import pytest

sys.path.insert(0, os.path.dirname(__file__))

# Override DATABASE_PATH BEFORE importing anything that reads config.
import config
_TEST_DB = os.path.join(config.BASE_DIR, "test_fileshare_t1.db")
config.DATABASE_PATH = _TEST_DB
config.JWT_SECRET_KEY = "test-secret-key-do-not-use-in-production"

from database import (
    init_db,
    get_connection,
    add_file,
    get_file_by_name,
    create_folder,
    get_folders_for_user,
    delete_folder,
    get_files_in_folder,
    set_file_visibility,
    add_file_share,
    remove_file_share,
    get_file_by_share_token,
    get_visible_files_for_user,
)
from auth import register_user


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


def test_schema_created_correctly():
    """Verify that folders and file_shares tables are created, and files columns are extended."""
    conn = get_connection()
    try:
        # Check folders table
        folders_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='folders'"
        ).fetchone()
        assert folders_exists is not None

        # Check file_shares table
        file_shares_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='file_shares'"
        ).fetchone()
        assert file_shares_exists is not None

        # Check files columns
        cursor = conn.execute("PRAGMA table_info(files)")
        columns = [row["name"] for row in cursor.fetchall()]
        assert "folder_id" in columns
        assert "visibility" in columns
        assert "share_token" in columns
    finally:
        conn.close()


def test_migration_idempotence():
    """Verify that calling init_db on an existing populated database does not error or lose data."""
    # 1. Create a user and a file
    u = register_user("alice", "alice@example.com", "password123")
    add_file("file_a.txt", "Original A", "text", 100, u["id"])
    
    # Verify they exist
    file_before = get_file_by_name("file_a.txt")
    assert file_before is not None
    assert file_before["visibility"] == "public"

    # 2. Run init_db again
    init_db()

    # 3. Verify data is intact
    file_after = get_file_by_name("file_a.txt")
    assert file_after is not None
    assert file_after["original_name"] == "Original A"
    assert file_after["visibility"] == "public"


def test_create_folder():
    """Verify folder creation works for top-level and nested folders, and checks constraints."""
    u1 = register_user("u1", "u1@example.com", "password123")
    
    # Create top-level folder
    f1_id = create_folder("Top Folder", u1["id"])
    assert f1_id > 0

    # Create nested folder
    f2_id = create_folder("Sub Folder", u1["id"], parent_folder_id=f1_id)
    assert f2_id > 0

    # Validate name constraints
    with pytest.raises(ValueError, match="Folder name must not be empty"):
        create_folder("", u1["id"])
    with pytest.raises(ValueError, match="Folder name must not be empty"):
        create_folder("   ", u1["id"])


def test_get_folders_for_user():
    """Verify get_folders_for_user filters correctly by owner and parent folder."""
    u1 = register_user("u1", "u1@example.com", "password123")
    u2 = register_user("u2", "u2@example.com", "password123")

    f1_id = create_folder("Folder A", u1["id"])
    f2_id = create_folder("Folder B", u1["id"])
    # Nested folder
    f3_id = create_folder("Folder C", u1["id"], parent_folder_id=f1_id)
    # Folder by different user
    f4_id = create_folder("Folder D", u2["id"])

    # User 1 top level folders
    u1_top = get_folders_for_user(u1["id"])
    assert len(u1_top) == 2
    names = [f["name"] for f in u1_top]
    assert "Folder A" in names
    assert "Folder B" in names

    # User 1 nested folders
    u1_nested = get_folders_for_user(u1["id"], parent_folder_id=f1_id)
    assert len(u1_nested) == 1
    assert u1_nested[0]["name"] == "Folder C"

    # User 2 top level folders
    u2_top = get_folders_for_user(u2["id"])
    assert len(u2_top) == 1
    assert u2_top[0]["name"] == "Folder D"


def test_delete_folder():
    """Verify delete_folder deletes folder, cascaded folders, and sets files folder_id to NULL."""
    u = register_user("u1", "u1@example.com", "password123")
    other_u = register_user("other", "other@example.com", "password123")
    
    f1_id = create_folder("Parent", u["id"])
    f2_id = create_folder("Child", u["id"], parent_folder_id=f1_id)

    # Add files to the folders
    add_file("file1.txt", "File 1", "text", 50, u["id"], folder_id=f1_id)
    add_file("file2.txt", "File 2", "text", 50, u["id"], folder_id=f2_id)

    # 1. Try to delete with wrong owner_id (should not delete)
    delete_folder(f1_id, other_u["id"])
    u_folders = get_folders_for_user(u["id"])
    assert len(u_folders) == 1 # Parent folder still exists

    # 2. Delete with correct owner_id
    delete_folder(f1_id, u["id"])

    # Parent folder and child folder should be deleted (cascade)
    assert len(get_folders_for_user(u["id"])) == 0
    assert len(get_folders_for_user(u["id"], parent_folder_id=f1_id)) == 0

    # Files should still exist but folder_id set to NULL
    f1 = get_file_by_name("file1.txt")
    f2 = get_file_by_name("file2.txt")
    assert f1 is not None
    assert f2 is not None
    assert f1["folder_id"] is None
    assert f2["folder_id"] is None


def test_set_file_visibility():
    """Verify visibility controls and checks owner_id."""
    u1 = register_user("u1", "u1@example.com", "password123")
    u2 = register_user("u2", "u2@example.com", "password123")

    add_file("file.txt", "File", "text", 50, u1["id"])

    # Set visibility to private
    set_file_visibility("file.txt", "private", u1["id"])
    f = get_file_by_name("file.txt")
    assert f["visibility"] == "private"

    # Fails for wrong owner
    with pytest.raises(ValueError, match="File not found or permission denied"):
        set_file_visibility("file.txt", "public", u2["id"])

    # Fails for invalid visibility
    with pytest.raises(ValueError, match="Visibility must be"):
        set_file_visibility("file.txt", "invalid_vis", u1["id"])


def test_add_and_remove_file_share():
    """Verify add_file_share, remove_file_share, idempotence, owner checks, and share token generation."""
    u1 = register_user("u1", "u1@example.com", "password123")
    u2 = register_user("u2", "u2@example.com", "password123")
    u3 = register_user("u3", "u3@example.com", "password123")

    add_file("file.txt", "File", "text", 50, u1["id"])
    assert get_file_by_name("file.txt")["share_token"] is None

    # Share file with u2
    add_file_share("file.txt", "u2", u1["id"])
    
    # Verify share token is generated
    f_after_share = get_file_by_name("file.txt")
    token = f_after_share["share_token"]
    assert token is not None

    # Idempotence: calling it again doesn't crash or create duplicates
    add_file_share("file.txt", "u2", u1["id"])

    # Verify u2 has access by querying db directly
    conn = get_connection()
    try:
        shares = conn.execute(
            "SELECT * FROM file_shares WHERE file_id = ?",
            (f_after_share["id"],)
        ).fetchall()
        assert len(shares) == 1
        assert shares[0]["shared_with_user_id"] == u2["id"]
    finally:
        conn.close()

    # Wrong owner tries to share (fails)
    with pytest.raises(ValueError, match="Permission denied"):
        add_file_share("file.txt", "u3", u2["id"])

    # Remove share
    remove_file_share("file.txt", "u2", u1["id"])
    
    conn = get_connection()
    try:
        shares = conn.execute(
            "SELECT * FROM file_shares WHERE file_id = ?",
            (f_after_share["id"],)
        ).fetchall()
        assert len(shares) == 0
    finally:
        conn.close()


def test_get_file_by_share_token():
    """Verify fetching file by share token."""
    u = register_user("u", "u@example.com", "password123")
    add_file("file.txt", "File", "text", 50, u["id"])

    # Initially token is None
    assert get_file_by_share_token("unknown_token") is None

    # Add share to generate token
    add_file_share("file.txt", "u", u["id"]) # sharing with self to trigger token generation
    token = get_file_by_name("file.txt")["share_token"]
    assert token is not None

    file_row = get_file_by_share_token(token)
    assert file_row is not None
    assert file_row["filename"] == "file.txt"


def test_get_visible_files_for_user():
    """Verify visible files logic: own files, public files, and shared files."""
    u1 = register_user("u1", "u1@example.com", "password123")
    u2 = register_user("u2", "u2@example.com", "password123")
    u3 = register_user("u3", "u3@example.com", "password123")

    # Add files for u1
    add_file("pub.txt", "Public", "text", 50, u1["id"], visibility="public")
    add_file("priv.txt", "Private", "text", 50, u1["id"], visibility="private")
    add_file("sh.txt", "Shared", "text", 50, u1["id"], visibility="shared")

    # Share sh.txt with u2 but not u3
    add_file_share("sh.txt", "u2", u1["id"])

    # u1 (owner) should see all three files
    u1_files = get_visible_files_for_user(u1["id"])
    assert len(u1_files) == 3
    filenames = [f["filename"] for f in u1_files]
    assert "pub.txt" in filenames
    assert "priv.txt" in filenames
    assert "sh.txt" in filenames

    # u2 (viewer, shared recipient) should see pub.txt and sh.txt
    u2_files = get_visible_files_for_user(u2["id"])
    assert len(u2_files) == 2
    filenames = [f["filename"] for f in u2_files]
    assert "pub.txt" in filenames
    assert "sh.txt" in filenames
    assert "priv.txt" not in filenames

    # u3 (viewer, not shared recipient) should see only pub.txt
    u3_files = get_visible_files_for_user(u3["id"])
    assert len(u3_files) == 1
    assert u3_files[0]["filename"] == "pub.txt"
