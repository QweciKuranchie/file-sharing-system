"""
Unit tests for primary_server.py TCP file server.
Validates PING, LIST, UPLOAD, DOWNLOAD, DELETE, and RENAME commands,
including duplicate renaming, directory traversal safety, and replication status.
"""

import os
import time
import socket
import threading
import json
import pytest
import shutil
from unittest.mock import patch

import primary_server

TEST_PORT = 9999
TEST_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "test_shared_files"))


@pytest.fixture(scope="module", autouse=True)
def setup_test_env():
    # Configure ports and directories for testing
    os.environ["PRIMARY_SERVER_PORT"] = str(TEST_PORT)
    os.environ["SHARED_FILES_DIR"] = TEST_DIR
    
    # Reload primary_server config to apply environment overrides
    primary_server.PORT = TEST_PORT
    primary_server.SHARED_FILES_DIR = TEST_DIR
    
    # Reset test directory
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    os.makedirs(TEST_DIR, exist_ok=True)
    
    # Start TCP server in a background thread
    server_thread = threading.Thread(target=primary_server.start_server, daemon=True)
    server_thread.start()
    time.sleep(0.5)  # Wait for socket to bind
    
    yield
    
    # Cleanup test files after suite completion
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)


def send_command(command: str, payload: bytes = None) -> str:
    """
    Helper to send a command line to the primary server and receive the response line.
    Optionally sends a payload if the server accepts it (e.g. UPLOAD's READY state).
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.connect(("127.0.0.1", TEST_PORT))
        sock.sendall(command.encode('utf-8'))
        
        # Read response line
        line_bytes = bytearray()
        while True:
            b = sock.recv(1)
            if not b or b == b'\n':
                break
            if b != b'\r':
                line_bytes.extend(b)
        response = line_bytes.decode('utf-8')
        
        # If payload is provided and server responds with READY, send it
        if payload is not None and response.strip() == "READY":
            sock.sendall(payload)
            
            # Read final response line
            line_bytes = bytearray()
            while True:
                b = sock.recv(1)
                if not b or b == b'\n':
                    break
                if b != b'\r':
                    line_bytes.extend(b)
            response = line_bytes.decode('utf-8')
            
        return response.strip()


def test_ping():
    resp = send_command("PING\n")
    assert resp == "OK PONG"


def test_unknown_command():
    resp = send_command("INVALIDCMD\n")
    assert resp == "ERROR UNKNOWN_COMMAND"


@patch('replication.replicate_file')
def test_upload_and_download(mock_replicate):
    mock_replicate.return_value = True
    
    content = b"Hello, this is a test file contents!"
    size = len(content)
    
    # 1. Test Upload
    resp = send_command(f"UPLOAD test.txt {size}\n", payload=content)
    assert resp == "OK FILE_SAVED test.txt"
    
    # Verify file saved to disk
    filepath = os.path.join(TEST_DIR, "test.txt")
    assert os.path.exists(filepath)
    with open(filepath, 'rb') as f:
        assert f.read() == content
        
    # 2. Test Download
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.connect(("127.0.0.1", TEST_PORT))
        sock.sendall(b"DOWNLOAD test.txt\n")
        
        # Read response header
        line_bytes = bytearray()
        while True:
            b = sock.recv(1)
            if not b or b == b'\n':
                break
            if b != b'\r':
                line_bytes.extend(b)
        header = line_bytes.decode('utf-8').strip()
        
        assert header == f"OK {size}"
        
        # Read file bytes
        downloaded = bytearray()
        while len(downloaded) < size:
            chunk = sock.recv(size - len(downloaded))
            if not chunk:
                break
            downloaded.extend(chunk)
            
        assert bytes(downloaded) == content


@patch('replication.replicate_file')
def test_upload_duplicate_rename(mock_replicate):
    mock_replicate.return_value = True
    
    content = b"Duplicate file contents"
    size = len(content)
    
    # Initial Upload
    resp = send_command(f"UPLOAD dup.txt {size}\n", payload=content)
    assert resp == "OK FILE_SAVED dup.txt"
    
    # Secondary Duplicate Upload
    resp2 = send_command(f"UPLOAD dup.txt {size}\n", payload=content)
    assert resp2 == "OK FILE_SAVED dup_1.txt"
    
    assert os.path.exists(os.path.join(TEST_DIR, "dup.txt"))
    assert os.path.exists(os.path.join(TEST_DIR, "dup_1.txt"))


@patch('replication.replicate_file')
def test_upload_replication_failed(mock_replicate):
    mock_replicate.return_value = False
    
    content = b"Failed replication contents"
    size = len(content)
    
    resp = send_command(f"UPLOAD fail_repl.txt {size}\n", payload=content)
    assert resp == "ERROR REPLICATION_FAILED"
    assert not os.path.exists(os.path.join(TEST_DIR, "fail_repl.txt"))


def test_download_not_found():
    resp = send_command("DOWNLOAD non_existent.txt\n")
    assert resp == "ERROR FILE_NOT_FOUND"


def test_download_directory_traversal():
    resp = send_command("DOWNLOAD ../auth.py\n")
    assert resp == "ERROR ACCESS_DENIED"


@patch('replication.replicate_file')
def test_list(mock_replicate):
    mock_replicate.return_value = True
    
    # Upload reference files
    send_command("UPLOAD list1.txt 5\n", payload=b"abcde")
    send_command("UPLOAD list2.txt 3\n", payload=b"xyz")
    
    resp = send_command("LIST\n")
    assert resp.startswith("OK ")
    
    json_str = resp[3:]
    files = json.loads(json_str)
    
    # Validate filenames and sizes
    names = [f["name"] for f in files]
    assert "list1.txt" in names
    assert "list2.txt" in names
    
    for f in files:
        if f["name"] == "list1.txt":
            assert f["size"] == 5
        elif f["name"] == "list2.txt":
            assert f["size"] == 3


@patch('replication.propagate_rename')
def test_rename(mock_propagate):
    mock_propagate.return_value = True
    
    filepath = os.path.join(TEST_DIR, "old.txt")
    with open(filepath, 'w') as f:
        f.write("rename test")
        
    resp = send_command("RENAME old.txt new.txt\n")
    assert resp == "OK FILE_RENAMED new.txt"
    assert not os.path.exists(filepath)
    assert os.path.exists(os.path.join(TEST_DIR, "new.txt"))


@patch('replication.propagate_rename')
def test_rename_conflict(mock_propagate):
    mock_propagate.return_value = True
    
    with open(os.path.join(TEST_DIR, "file1.txt"), 'w') as f:
        f.write("1")
    with open(os.path.join(TEST_DIR, "file2.txt"), 'w') as f:
        f.write("2")
        
    resp = send_command("RENAME file1.txt file2.txt\n")
    assert resp == "ERROR NAME_CONFLICT"


@patch('replication.propagate_delete')
def test_delete(mock_propagate):
    mock_propagate.return_value = True
    
    filepath = os.path.join(TEST_DIR, "delete_me.txt")
    with open(filepath, 'w') as f:
        f.write("delete test")
        
    resp = send_command("DELETE delete_me.txt\n")
    assert resp == "OK FILE_DELETED"
    assert not os.path.exists(filepath)


def test_delete_not_found():
    resp = send_command("DELETE no_exist.txt\n")
    assert resp == "ERROR FILE_NOT_FOUND"


@patch('replication.replicate_file')
def test_concurrent_duplicate_uploads(mock_replicate):
    mock_replicate.return_value = True
    
    num_threads = 10
    filename = "concurrent.txt"
    content = b"Concurrent upload content"
    size = len(content)
    
    results = []
    
    def worker():
        try:
            resp = send_command(f"UPLOAD {filename} {size}\n", payload=content)
            results.append(resp)
        except Exception as e:
            results.append(str(e))
            
    threads = []
    for _ in range(num_threads):
        t = threading.Thread(target=worker)
        threads.append(t)
        
    for t in threads:
        t.start()
        
    for t in threads:
        t.join()
        
    assert len(results) == num_threads
    
    saved_names = []
    for resp in results:
        assert resp.startswith("OK FILE_SAVED ")
        saved_name = resp.split(" ")[-1]
        saved_names.append(saved_name)
        
    assert len(set(saved_names)) == num_threads
    
    for name in saved_names:
        filepath = os.path.join(TEST_DIR, name)
        assert os.path.exists(filepath)
        with open(filepath, 'rb') as f:
            assert f.read() == content


@patch('replication.propagate_delete')
def test_delete_replication_failed(mock_propagate):
    mock_propagate.return_value = False
    
    filename = "del_fail.txt"
    filepath = os.path.join(TEST_DIR, filename)
    with open(filepath, 'wb') as f:
        f.write(b"Delete fail content")
        
    resp = send_command(f"DELETE {filename}\n")
    assert resp == "ERROR REPLICATION_FAILED"
    assert os.path.exists(filepath)


@patch('replication.propagate_rename')
def test_rename_replication_failed(mock_propagate):
    mock_propagate.return_value = False
    
    old_name = "rename_fail_old.txt"
    new_name = "rename_fail_new.txt"
    old_path = os.path.join(TEST_DIR, old_name)
    new_path = os.path.join(TEST_DIR, new_name)
    
    with open(old_path, 'wb') as f:
        f.write(b"Rename fail content")
        
    resp = send_command(f"RENAME {old_name} {new_name}\n")
    assert resp == "ERROR REPLICATION_FAILED"
    
    assert os.path.exists(old_path)
    assert not os.path.exists(new_path)


def test_upload_negative_size():
    resp = send_command("UPLOAD neg_size.txt -5\n")
    assert resp == "ERROR INVALID_SIZE"
    assert not os.path.exists(os.path.join(TEST_DIR, "neg_size.txt"))


def test_upload_truncated():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.connect(("127.0.0.1", TEST_PORT))
        sock.sendall(b"UPLOAD trunc.txt 100\n")
        
        line_bytes = bytearray()
        while True:
            b = sock.recv(1)
            if not b or b == b'\n':
                break
            if b != b'\r':
                line_bytes.extend(b)
        response = line_bytes.decode('utf-8').strip()
        assert response == "READY"
        
        sock.sendall(b"a" * 20)
        sock.close()
        
    # Give the server a brief moment to finish thread processing and clean up
    time.sleep(0.1)
    assert not os.path.exists(os.path.join(TEST_DIR, "trunc.txt"))


@patch('replication.propagate_rename')
def test_rename_with_spaces(mock_propagate):
    mock_propagate.return_value = True
    
    old_name = "old name with spaces.txt"
    new_name = "new name with spaces.txt"
    old_path = os.path.join(TEST_DIR, old_name)
    new_path = os.path.join(TEST_DIR, new_name)
    
    with open(old_path, 'wb') as f:
        f.write(b"Spaces content")
        
    resp = send_command(f'RENAME "{old_name}" "{new_name}"\n')
    assert resp == f"OK FILE_RENAMED {new_name}"
    
    assert not os.path.exists(old_path)
    assert os.path.exists(new_path)
    with open(new_path, 'rb') as f:
        assert f.read() == b"Spaces content"


@patch('replication.propagate_rename')
def test_rename_spaces_mixed(mock_propagate):
    mock_propagate.return_value = True
    
    old_name = "old spaces.txt"
    new_name = "new_no_spaces.txt"
    old_path = os.path.join(TEST_DIR, old_name)
    new_path = os.path.join(TEST_DIR, new_name)
    with open(old_path, 'wb') as f:
        f.write(b"Mixed 1")
    resp = send_command(f'RENAME "{old_name}" {new_name}\n')
    assert resp == f"OK FILE_RENAMED {new_name}"
    assert not os.path.exists(old_path)
    assert os.path.exists(new_path)
    
    old_name2 = "old_no_spaces2.txt"
    new_name2 = "new spaces2.txt"
    old_path2 = os.path.join(TEST_DIR, old_name2)
    new_path2 = os.path.join(TEST_DIR, new_name2)
    with open(old_path2, 'wb') as f:
        f.write(b"Mixed 2")
    resp2 = send_command(f'RENAME {old_name2} "{new_name2}"\n')
    assert resp2 == f"OK FILE_RENAMED {new_name2}"
    assert not os.path.exists(old_path2)
    assert os.path.exists(new_path2)
