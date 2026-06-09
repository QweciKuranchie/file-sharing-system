"""
primary_server.py — The Primary TCP Server for the distributed file sharing system.

This server handles all incoming client file operations (routed through Flask)
and initiates replication. It implements the custom line-based TCP socket protocol.
"""

import socket
import threading
import os
import json
import logging
from typing import Tuple

from config import PRIMARY_SERVER_HOST, PRIMARY_SERVER_PORT, SHARED_FILES_DIR

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("primary_server")


# Note: get_unique_filename has been replaced by inline atomic file creation in handle_client UPLOAD handler.


def format_size(size_bytes: int) -> str:
    """Format size in bytes to a human-readable string matching Protocol Spec."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    
    for unit in ['KB', 'MB', 'GB', 'TB']:
        size_bytes /= 1024.0
        if size_bytes < 1024.0:
            if size_bytes == int(size_bytes):
                return f"{int(size_bytes)} {unit}"
            return f"{size_bytes:.1f} {unit}"
    return f"{size_bytes:.1f} PB"


def handle_client(client_sock: socket.socket, addr: Tuple[str, int]) -> None:
    """Handle individual client connections in a dedicated thread."""
    logger.info(f"Accepted connection from {addr}")
    client_sock.settimeout(60.0)
    
    try:
        buffer = bytearray()
        while True:
            # Read until we have a newline character in the buffer
            while b'\n' not in buffer:
                data = client_sock.recv(4096)
                if not data:
                    break
                buffer.extend(data)
            
            if b'\n' not in buffer:
                # Connection closed by client
                break
            
            # Split the first line out of the buffer
            line_bytes, remaining = buffer.split(b'\n', 1)
            buffer = bytearray(remaining)
            
            # Parse line as UTF-8 string
            line = line_bytes.decode('utf-8').rstrip('\r\n')
            if not line:
                continue
            
            parts = line.split()
            if not parts:
                continue
            
            cmd = parts[0].upper()
            
            if cmd == 'PING':
                client_sock.sendall(b"OK PONG\n")
                
            elif cmd == 'UPLOAD':
                if len(parts) < 3:
                    client_sock.sendall(b"ERROR INVALID_COMMAND\n")
                    continue
                
                filename = parts[1]
                try:
                    size = int(parts[2])
                    if size < 0:
                        client_sock.sendall(b"ERROR INVALID_FILE_SIZE\n")
                        continue
                except ValueError:
                    client_sock.sendall(b"ERROR INVALID_COMMAND\n")
                    continue
                
                # Sanitize filename to prevent directory traversal
                filename = os.path.basename(filename)
                if not filename:
                    client_sock.sendall(b"ERROR INVALID_FILENAME\n")
                    continue
                
                client_sock.sendall(b"READY\n")
                
                # Read exactly 'size' bytes of binary data
                remaining_bytes = size
                file_data = bytearray()
                while remaining_bytes > 0:
                    if buffer:
                        chunk = buffer[:remaining_bytes]
                        file_data.extend(chunk)
                        remaining_bytes -= len(chunk)
                        buffer = buffer[len(chunk):]
                    else:
                        chunk_size = min(remaining_bytes, 4096)
                        chunk = client_sock.recv(chunk_size)
                        if not chunk:
                            break
                        file_data.extend(chunk)
                        remaining_bytes -= len(chunk)
                
                if remaining_bytes > 0:
                    logger.warning(f"Client {addr} disconnected before completing UPLOAD")
                    client_sock.sendall(b"ERROR INCOMPLETE_UPLOAD\n")
                    continue
                
                # Ensure directory exists
                os.makedirs(SHARED_FILES_DIR, exist_ok=True)
                
                # Deduplicate filename atomically using 'xb' mode
                base, ext = os.path.splitext(filename)
                counter = 0
                saved_name = None
                
                try:
                    while True:
                        if counter == 0:
                            candidate_name = filename
                        else:
                            candidate_name = f"{base}_{counter}{ext}"
                        
                        filepath = os.path.join(SHARED_FILES_DIR, candidate_name)
                        try:
                            # 'xb' mode creates the file atomically; raises FileExistsError if it already exists
                            with open(filepath, 'xb') as f:
                                f.write(file_data)
                            saved_name = candidate_name
                            break
                        except FileExistsError:
                            counter += 1
                    
                    # Call replication module with consistency checks
                    replicated = False
                    try:
                        import replication
                        replicated = replication.replicate_file(saved_name, size)
                    except Exception as rep_err:
                        logger.error(f"Replication failed to trigger for {saved_name}: {rep_err}")
                    
                    if replicated:
                        client_sock.sendall(f"OK FILE_SAVED {saved_name}\n".encode('utf-8'))
                        logger.info(f"File saved and replicated successfully: {saved_name} ({size} bytes)")
                    else:
                        client_sock.sendall(f"ERROR REPLICATION_FAILED {saved_name}\n".encode('utf-8'))
                        logger.warning(f"File saved on primary but replication failed: {saved_name}")
                except Exception as save_err:
                    logger.error(f"Failed to save file {saved_name}: {save_err}")
                    client_sock.sendall(b"ERROR FILE_SAVE_FAILED\n")
                    
            elif cmd == 'DOWNLOAD':
                if len(parts) < 2:
                    client_sock.sendall(b"ERROR INVALID_COMMAND\n")
                    continue
                
                filename = os.path.basename(parts[1])
                filepath = os.path.join(SHARED_FILES_DIR, filename)
                
                if not os.path.exists(filepath) or not os.path.isfile(filepath):
                    client_sock.sendall(b"ERROR FILE_NOT_FOUND\n")
                    continue
                
                # Pre-stream step: file open and size lookup
                file_to_send = None
                try:
                    size = os.path.getsize(filepath)
                    file_to_send = open(filepath, 'rb')
                except Exception as pre_err:
                    logger.error(f"Pre-stream lookup failed for {filename}: {pre_err}")
                    client_sock.sendall(b"ERROR DOWNLOAD_FAILED\n")
                    if file_to_send:
                        file_to_send.close()
                    continue
                
                # Stream step
                try:
                    client_sock.sendall(f"OK {size}\n".encode('utf-8'))
                    with file_to_send as f:
                        while True:
                            chunk = f.read(4096)
                            if not chunk:
                                break
                            client_sock.sendall(chunk)
                    logger.info(f"Downloaded file: {filename} ({size} bytes) to {addr}")
                except Exception as stream_err:
                    logger.error(f"Streaming failed after header sent for {filename} to {addr}: {stream_err}")
                    try:
                        client_sock.close()
                    except OSError:
                        pass
                    break
                    
            elif cmd == 'LIST':
                try:
                    files = []
                    if os.path.exists(SHARED_FILES_DIR):
                        for name in os.listdir(SHARED_FILES_DIR):
                            path = os.path.join(SHARED_FILES_DIR, name)
                            if os.path.isfile(path):
                                size = os.path.getsize(path)
                                files.append({
                                    "name": name,
                                    "size": format_size(size)
                                })
                    
                    json_str = json.dumps(files)
                    client_sock.sendall(f"OK {json_str}\n".encode('utf-8'))
                except Exception as list_err:
                    logger.error(f"List error: {list_err}")
                    client_sock.sendall(b"ERROR CANNOT_LIST\n")
                    
            elif cmd == 'DELETE':
                if len(parts) < 2:
                    client_sock.sendall(b"ERROR INVALID_COMMAND\n")
                    continue
                
                filename = os.path.basename(parts[1])
                filepath = os.path.join(SHARED_FILES_DIR, filename)
                
                if not os.path.exists(filepath) or not os.path.isfile(filepath):
                    client_sock.sendall(b"ERROR FILE_NOT_FOUND\n")
                    continue
                
                try:
                    # Move to temporary name to allow rollback on replication failure
                    temp_filepath = filepath + ".tmp_del"
                    if os.path.exists(temp_filepath):
                        os.remove(temp_filepath)
                    os.rename(filepath, temp_filepath)
                    
                    replicated = False
                    try:
                        import replication
                        replicated = replication.propagate_delete(filename)
                    except Exception as rep_err:
                        logger.error(f"Replication deletion failed for {filename}: {rep_err}")
                    
                    if replicated:
                        os.remove(temp_filepath)
                        client_sock.sendall(b"OK FILE_DELETED\n")
                        logger.info(f"Deleted file and propagated deletion: {filename}")
                    else:
                        # Rollback local delete
                        os.rename(temp_filepath, filepath)
                        client_sock.sendall(b"ERROR REPLICATION_FAILED\n")
                        logger.warning(f"Deletion failed to propagate, rolled back delete: {filename}")
                except Exception as del_err:
                    logger.error(f"Delete failed for {filename}: {del_err}")
                    client_sock.sendall(b"ERROR DELETE_FAILED\n")
                    
            elif cmd == 'RENAME':
                if len(parts) < 3:
                    client_sock.sendall(b"ERROR INVALID_COMMAND\n")
                    continue
                
                old_name = os.path.basename(parts[1])
                new_name = os.path.basename(parts[2])
                
                old_path = os.path.join(SHARED_FILES_DIR, old_name)
                new_path = os.path.join(SHARED_FILES_DIR, new_name)
                
                if not os.path.exists(old_path) or not os.path.isfile(old_path):
                    client_sock.sendall(b"ERROR FILE_NOT_FOUND\n")
                    continue
                
                if os.path.exists(new_path):
                    client_sock.sendall(b"ERROR NAME_CONFLICT\n")
                    continue
                
                try:
                    os.rename(old_path, new_path)
                    
                    replicated = False
                    try:
                        import replication
                        replicated = replication.propagate_rename(old_name, new_name)
                    except Exception as rep_err:
                        logger.error(f"Replication rename propagation failed: {rep_err}")
                    
                    if replicated:
                        client_sock.sendall(f"OK FILE_RENAMED {new_name}\n".encode('utf-8'))
                        logger.info(f"Renamed file '{old_name}' to '{new_name}' and propagated")
                    else:
                        # Rollback rename
                        os.rename(new_path, old_path)
                        client_sock.sendall(b"ERROR REPLICATION_FAILED\n")
                        logger.warning(f"Rename propagation failed, rolled back rename: '{old_name}'")
                except Exception as ren_err:
                    logger.error(f"Rename failed from {old_name} to {new_name}: {ren_err}")
                    client_sock.sendall(b"ERROR RENAME_FAILED\n")
                    
            else:
                client_sock.sendall(b"ERROR UNKNOWN_COMMAND\n")
                logger.warning(f"Unknown command received from {addr}: {cmd}")
                
    except socket.timeout:
        logger.warning(f"Connection timeout with client {addr}")
    except Exception as err:
        logger.error(f"Error handling client {addr}: {err}")
    finally:
        try:
            client_sock.close()
        except OSError:
            pass
        logger.info(f"Closed connection from {addr}")


def main() -> None:
    # Ensure sharing storage exists on startup
    os.makedirs(SHARED_FILES_DIR, exist_ok=True)
    
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    
    try:
        server.bind((PRIMARY_SERVER_HOST, PRIMARY_SERVER_PORT))
    except Exception as bind_err:
        logger.critical(f"Could not bind server to {PRIMARY_SERVER_HOST}:{PRIMARY_SERVER_PORT}: {bind_err}")
        return
        
    server.listen()
    logger.info(f"Primary TCP Server running on {PRIMARY_SERVER_HOST}:{PRIMARY_SERVER_PORT}...")
    
    try:
        while True:
            try:
                client_sock, addr = server.accept()
                thread = threading.Thread(target=handle_client, args=(client_sock, addr))
                thread.daemon = True
                thread.start()
            except Exception as loop_err:
                logger.error(f"Error in server accept loop: {loop_err}")
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received. Shutting down server...")
    finally:
        server.close()
        logger.info("Server socket closed.")


if __name__ == '__main__':
    main()
