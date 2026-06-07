"""
Primary TCP Server for the Distributed File-Sharing System.
Listens on port 9000 and handles file uploads, downloads, listings, renames,
and deletions concurrently using threads, propagating writes to the replica server.
"""

import os
import sys
import socket
import threading
import json
import traceback
import replication
import shlex

HOST = "0.0.0.0"
PORT = int(os.environ.get("PRIMARY_SERVER_PORT", 9000))
SHARED_FILES_DIR = os.path.abspath(os.environ.get("SHARED_FILES_DIR", os.path.join(os.path.dirname(__file__), "shared_files")))

# Ensure shared directory exists on startup
if not os.path.exists(SHARED_FILES_DIR):
    os.makedirs(SHARED_FILES_DIR, exist_ok=True)
    print(f"Created shared directory: {SHARED_FILES_DIR}")

# Shared lock to make filename reservation atomic during concurrent uploads
UPLOAD_LOCK = threading.Lock()


class SocketBuffer:
    """
    A helper buffer to read lines and raw bytes from a TCP socket
    without losing binary data.
    """
    def __init__(self, sock):
        self.sock = sock
        self.buffer = bytearray()
        
    def read_line(self) -> str:
        """
        Reads a single newline-terminated line from the socket.
        """
        while b'\n' not in self.buffer:
            data = self.sock.recv(1024)
            if not data:
                if self.buffer:
                    line = self.buffer.decode('utf-8', errors='ignore')
                    self.buffer.clear()
                    return line
                return None
            self.buffer.extend(data)
            
        newline_idx = self.buffer.index(b'\n')
        line_bytes = self.buffer[:newline_idx]
        self.buffer = self.buffer[newline_idx + 1:]
        
        line = line_bytes.decode('utf-8', errors='ignore')
        if line.endswith('\r'):
            line = line[:-1]
        return line
        
    def read_bytes(self, n: int) -> bytes:
        """
        Reads exactly n bytes from the socket.
        """
        if n < 0:
            raise ValueError("Byte count cannot be negative")
            
        if len(self.buffer) >= n:
            res = bytes(self.buffer[:n])
            self.buffer = self.buffer[n:]
            return res
            
        res = bytearray(self.buffer)
        self.buffer.clear()
        
        needed = n - len(res)
        while needed > 0:
            data = self.sock.recv(min(needed, 65536))
            if not data:
                return None
            res.extend(data)
            needed -= len(data)
            
        return bytes(res)


def get_unique_filename(directory: str, filename: str) -> str:
    """
    Generates a unique filename in the given directory by appending '_1', '_2', etc.,
    before the extension if the file already exists.
    Note: This should be called under UPLOAD_LOCK to ensure atomic reservation.
    """
    filepath = os.path.join(directory, filename)
    if not os.path.exists(filepath):
        return filename
        
    base, ext = os.path.splitext(filename)
    counter = 1
    while True:
        new_filename = f"{base}_{counter}{ext}"
        new_filepath = os.path.join(directory, new_filename)
        if not os.path.exists(new_filepath):
            return new_filename
        counter += 1


def is_safe_path(filename: str) -> bool:
    """
    Prevents directory traversal attacks by verifying that the resolved path
    is inside the SHARED_FILES_DIR.
    """
    filepath = os.path.join(SHARED_FILES_DIR, filename)
    resolved_path = os.path.abspath(filepath)
    try:
        prefix = os.path.commonpath([SHARED_FILES_DIR, resolved_path])
        return prefix == SHARED_FILES_DIR
    except ValueError:
        return False


def handle_client(client_socket, client_address):
    """
    Client thread connection handler. Parses and executes socket protocol commands.
    """
    print(f"[+] Connection established from {client_address[0]}:{client_address[1]}")
    buffer = SocketBuffer(client_socket)
    
    try:
        while True:
            line = buffer.read_line()
            if line is None:
                break
                
            line = line.strip()
            if not line:
                continue
                
            parts = line.split()
            if not parts:
                continue
                
            cmd = parts[0].upper()
            
            if cmd == "PING":
                client_socket.sendall(b"OK PONG\n")
                
            elif cmd == "LIST":
                files_list = []
                if os.path.exists(SHARED_FILES_DIR):
                    for name in os.listdir(SHARED_FILES_DIR):
                        path = os.path.join(SHARED_FILES_DIR, name)
                        if os.path.isfile(path):
                            files_list.append({
                                "name": name,
                                "size": os.path.getsize(path)
                            })
                response = f"OK {json.dumps(files_list)}\n"
                client_socket.sendall(response.encode('utf-8'))
                
            elif cmd == "UPLOAD":
                if len(parts) < 3:
                    client_socket.sendall(b"ERROR MALFORMED_COMMAND\n")
                    continue
                    
                try:
                    size = int(parts[-1])
                    filename = " ".join(parts[1:-1])
                except ValueError:
                    client_socket.sendall(b"ERROR INVALID_SIZE\n")
                    continue
                    
                if size < 0:
                    client_socket.sendall(b"ERROR INVALID_SIZE\n")
                    continue
                    
                if not is_safe_path(filename):
                    client_socket.sendall(b"ERROR ACCESS_DENIED\n")
                    continue
                    
                # Get unique filename for duplicate resolution under lock to ensure atomicity
                with UPLOAD_LOCK:
                    saved_name = get_unique_filename(SHARED_FILES_DIR, filename)
                    filepath = os.path.join(SHARED_FILES_DIR, saved_name)
                    # Atomically reserve the file path by creating an empty file
                    # so that subsequent calls to get_unique_filename see it as existing.
                    try:
                        with open(filepath, 'wb') as f:
                            pass
                    except Exception as e:
                        client_socket.sendall(f"ERROR SAVE_FAILED {str(e)}\n".encode('utf-8'))
                        continue
                
                # Signal ready to receive bytes
                client_socket.sendall(b"READY\n")
                
                # Receive size bytes
                try:
                    file_bytes = buffer.read_bytes(size)
                except ValueError:
                    file_bytes = None
                
                if file_bytes is None or len(file_bytes) < size:
                    # Clean up the reserved file
                    try:
                        os.remove(filepath)
                    except Exception:
                        pass
                    client_socket.sendall(b"ERROR INCOMPLETE_DATA\n")
                    continue
                    
                # Save to disk
                try:
                    with open(filepath, 'wb') as f:
                        f.write(file_bytes)
                except Exception as e:
                    # Clean up the reserved file
                    try:
                        os.remove(filepath)
                    except Exception:
                        pass
                    client_socket.sendall(f"ERROR SAVE_FAILED {str(e)}\n".encode('utf-8'))
                    continue
                    
                # Replicate to the replica server
                repl_success = replication.replicate_file(saved_name, filepath)
                if repl_success:
                    client_socket.sendall(f"OK FILE_SAVED {saved_name}\n".encode('utf-8'))
                else:
                    # Rollback the local mutation on replication failure
                    try:
                        os.remove(filepath)
                    except Exception:
                        pass
                    client_socket.sendall(b"ERROR REPLICATION_FAILED\n")
                    
            elif cmd == "DOWNLOAD":
                if len(parts) < 2:
                    client_socket.sendall(b"ERROR MALFORMED_COMMAND\n")
                    continue
                    
                # Reconstruct full filename in case of spaces
                filename = line[9:].strip()
                if not is_safe_path(filename):
                    client_socket.sendall(b"ERROR ACCESS_DENIED\n")
                    continue
                    
                filepath = os.path.join(SHARED_FILES_DIR, filename)
                if not os.path.exists(filepath) or not os.path.isfile(filepath):
                    client_socket.sendall(b"ERROR FILE_NOT_FOUND\n")
                else:
                    try:
                        size = os.path.getsize(filepath)
                        client_socket.sendall(f"OK {size}\n".encode('utf-8'))
                        with open(filepath, 'rb') as f:
                            while True:
                                chunk = f.read(65536)
                                if not chunk:
                                    break
                                client_socket.sendall(chunk)
                    except Exception as e:
                        print(f"[-] Error serving download for {filename}: {e}")
                        
            elif cmd == "DELETE":
                if len(parts) < 2:
                    client_socket.sendall(b"ERROR MALFORMED_COMMAND\n")
                    continue
                    
                filename = line[7:].strip()
                if not is_safe_path(filename):
                    client_socket.sendall(b"ERROR ACCESS_DENIED\n")
                    continue
                    
                filepath = os.path.join(SHARED_FILES_DIR, filename)
                if not os.path.exists(filepath) or not os.path.isfile(filepath):
                    client_socket.sendall(b"ERROR FILE_NOT_FOUND\n")
                else:
                    # Transactional delete: rename to a temp name, propagate, then delete or rollback
                    temp_filepath = filepath + f".{threading.get_ident()}.tmp_delete"
                    try:
                        os.rename(filepath, temp_filepath)
                    except Exception as e:
                        client_socket.sendall(f"ERROR DELETE_FAILED {str(e)}\n".encode('utf-8'))
                        continue
                        
                    # Propagate delete operation to replica
                    repl_success = replication.propagate_delete(filename)
                    if repl_success:
                        try:
                            os.remove(temp_filepath)
                        except Exception:
                            pass
                        client_socket.sendall(b"OK FILE_DELETED\n")
                    else:
                        # Rollback the local deletion
                        try:
                            os.rename(temp_filepath, filepath)
                        except Exception:
                            pass
                        client_socket.sendall(b"ERROR REPLICATION_FAILED\n")
                        
            elif cmd == "RENAME":
                # We use shlex.split to parse RENAME to handle filenames with spaces correctly
                try:
                    rename_parts = shlex.split(line)
                except ValueError:
                    client_socket.sendall(b"ERROR MALFORMED_COMMAND\n")
                    continue
                    
                if len(rename_parts) < 3:
                    client_socket.sendall(b"ERROR MALFORMED_COMMAND\n")
                    continue
                    
                old_name = rename_parts[1]
                new_name = rename_parts[2]
                
                if not is_safe_path(old_name) or not is_safe_path(new_name):
                    client_socket.sendall(b"ERROR ACCESS_DENIED\n")
                    continue
                    
                old_path = os.path.join(SHARED_FILES_DIR, old_name)
                new_path = os.path.join(SHARED_FILES_DIR, new_name)
                
                if not os.path.exists(old_path) or not os.path.isfile(old_path):
                    client_socket.sendall(b"ERROR FILE_NOT_FOUND\n")
                elif os.path.exists(new_path):
                    client_socket.sendall(b"ERROR NAME_CONFLICT\n")
                else:
                    try:
                        os.rename(old_path, new_path)
                    except Exception as e:
                        client_socket.sendall(f"ERROR RENAME_FAILED {str(e)}\n".encode('utf-8'))
                        continue
                        
                    # Propagate rename operation to replica
                    repl_success = replication.propagate_rename(old_name, new_name)
                    if repl_success:
                        client_socket.sendall(f"OK FILE_RENAMED {new_name}\n".encode('utf-8'))
                    else:
                        # Rollback the local rename
                        try:
                            os.rename(new_path, old_path)
                        except Exception:
                            pass
                        client_socket.sendall(b"ERROR REPLICATION_FAILED\n")
            else:
                client_socket.sendall(b"ERROR UNKNOWN_COMMAND\n")
                
    except Exception as e:
        print(f"[-] Error handling client {client_address}: {e}")
    finally:
        client_socket.close()
        print(f"[-] Connection with {client_address[0]}:{client_address[1]} closed.")


def start_server():
    """
    Initializes and starts the TCP server listening on port 9000.
    """
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    
    try:
        server_socket.bind((HOST, PORT))
        server_socket.listen(50)
        print(f"[*] Primary TCP Server listening on {HOST}:{PORT}")
        
        while True:
            client_socket, client_address = server_socket.accept()
            client_thread = threading.Thread(
                target=handle_client,
                args=(client_socket, client_address),
                daemon=True
            )
            client_thread.start()
    except KeyboardInterrupt:
        print("\n[*] Shutting down Primary TCP Server.")
    except Exception as e:
        print(f"[-] Server error: {e}")
        traceback.print_exc()
    finally:
        server_socket.close()


if __name__ == "__main__":
    start_server()
