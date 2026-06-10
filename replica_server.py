"""
replica_server.py — The Replica TCP Server for the distributed file sharing system.

This server acts as the replication target. It receives files from the primary
server via the REPLICATE command and can serve files during failover via
DOWNLOAD and LIST. Direct client UPLOAD commands are rejected.
"""

import socket
import threading
import os
import json
import logging
from typing import Tuple

from config import REPLICA_SERVER_HOST, REPLICA_SERVER_PORT, REPLICA_SHARED_FILES_DIR

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("replica_server")


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
        
        # Read the first line which MUST be the AUTH command
        while b'\n' not in buffer:
            data = client_sock.recv(4096)
            if not data:
                break
            buffer.extend(data)
            
        if b'\n' not in buffer:
            return
            
        line_bytes, remaining = buffer.split(b'\n', 1)
        buffer = bytearray(remaining)
        
        line = line_bytes.decode('utf-8').rstrip('\r\n')
        parts = line.split()
        if not parts or parts[0].upper() != 'AUTH' or len(parts) < 2:
            client_sock.sendall(b"ERROR UNAUTHORIZED\n")
            return
            
        from config import TCP_CLIENT_SECRET, TCP_REPLICATION_SECRET
        role = None
        if parts[1] == TCP_REPLICATION_SECRET:
            role = "replication"
        elif parts[1] == TCP_CLIENT_SECRET:
            role = "client"
        else:
            client_sock.sendall(b"ERROR UNAUTHORIZED\n")
            return
            
        client_sock.sendall(b"OK AUTHENTICATED\n")
        
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
                # Replica is read-only for direct client uploads
                client_sock.sendall(b"ERROR WRITE_NOT_ALLOWED\n")
                logger.warning(
                    f"Rejected UPLOAD attempt from {addr} — "
                    f"replica is read-only"
                )

            elif cmd == 'REPLICATE':
                if role != "replication":
                    client_sock.sendall(b"ERROR WRITE_NOT_ALLOWED\n")
                    continue
                # Receive a file from the primary server
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

                # Signal primary that we are ready to receive
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
                    logger.warning(
                        f"Primary disconnected before completing REPLICATE "
                        f"for '{filename}'"
                    )
                    client_sock.sendall(b"ERROR INCOMPLETE_REPLICATION\n")
                    continue

                # Ensure directory exists and save the file
                os.makedirs(REPLICA_SHARED_FILES_DIR, exist_ok=True)
                filepath = os.path.join(REPLICA_SHARED_FILES_DIR, filename)

                try:
                    with open(filepath, 'wb') as f:
                        f.write(file_data)

                    client_sock.sendall(b"OK REPLICATED\n")
                    logger.info(
                        f"Replicated file: '{filename}' ({size} bytes) "
                        f"from {addr}"
                    )
                except Exception as save_err:
                    logger.error(
                        f"Failed to save replicated file '{filename}': "
                        f"{save_err}"
                    )
                    client_sock.sendall(b"ERROR REPLICATION_SAVE_FAILED\n")

            elif cmd == 'DOWNLOAD':
                if len(parts) < 2:
                    client_sock.sendall(b"ERROR INVALID_COMMAND\n")
                    continue

                filename = os.path.basename(parts[1])
                filepath = os.path.join(REPLICA_SHARED_FILES_DIR, filename)

                if not os.path.exists(filepath) or not os.path.isfile(filepath):
                    client_sock.sendall(b"ERROR FILE_NOT_FOUND\n")
                    continue

                # Pre-stream step: file open and size lookup
                file_to_send = None
                try:
                    size = os.path.getsize(filepath)
                    file_to_send = open(filepath, 'rb')
                except Exception as pre_err:
                    logger.error(
                        f"Pre-stream lookup failed for '{filename}': "
                        f"{pre_err}"
                    )
                    client_sock.sendall(b"ERROR DOWNLOAD_FAILED\n")
                    if file_to_send:
                        file_to_send.close()
                    continue

                # Stream step
                try:
                    client_sock.sendall(
                        f"OK {size}\n".encode('utf-8')
                    )
                    with file_to_send as f:
                        while True:
                            chunk = f.read(4096)
                            if not chunk:
                                break
                            client_sock.sendall(chunk)
                    logger.info(
                        f"Downloaded file: '{filename}' ({size} bytes) "
                        f"to {addr}"
                    )
                except Exception as stream_err:
                    logger.error(
                        f"Streaming failed after header sent for "
                        f"'{filename}' to {addr}: {stream_err}"
                    )
                    try:
                        client_sock.close()
                    except OSError:
                        pass
                    break

            elif cmd == 'LIST':
                try:
                    files = []
                    if os.path.exists(REPLICA_SHARED_FILES_DIR):
                        for name in os.listdir(REPLICA_SHARED_FILES_DIR):
                            path = os.path.join(
                                REPLICA_SHARED_FILES_DIR, name
                            )
                            if os.path.isfile(path):
                                size = os.path.getsize(path)
                                files.append({
                                    "name": name,
                                    "size": format_size(size),
                                })

                    json_str = json.dumps(files)
                    client_sock.sendall(
                        f"OK {json_str}\n".encode('utf-8')
                    )
                except Exception as list_err:
                    logger.error(f"List error: {list_err}")
                    client_sock.sendall(b"ERROR CANNOT_LIST\n")

            elif cmd == 'DELETE':
                if role != "replication":
                    client_sock.sendall(b"ERROR WRITE_NOT_ALLOWED\n")
                    continue
                if len(parts) < 2:
                    client_sock.sendall(b"ERROR INVALID_COMMAND\n")
                    continue

                filename = os.path.basename(parts[1])
                filepath = os.path.join(REPLICA_SHARED_FILES_DIR, filename)

                if not os.path.exists(filepath) or not os.path.isfile(filepath):
                    client_sock.sendall(b"ERROR FILE_NOT_FOUND\n")
                    continue

                try:
                    os.remove(filepath)
                    client_sock.sendall(b"OK FILE_DELETED\n")
                    logger.info(f"Deleted replicated file: '{filename}'")
                except Exception as del_err:
                    logger.error(
                        f"Delete failed for '{filename}': {del_err}"
                    )
                    client_sock.sendall(b"ERROR DELETE_FAILED\n")

            elif cmd == 'RENAME':
                if role != "replication":
                    client_sock.sendall(b"ERROR WRITE_NOT_ALLOWED\n")
                    continue
                if len(parts) < 3:
                    client_sock.sendall(b"ERROR INVALID_COMMAND\n")
                    continue

                old_name = os.path.basename(parts[1])
                new_name = os.path.basename(parts[2])

                old_path = os.path.join(REPLICA_SHARED_FILES_DIR, old_name)
                new_path = os.path.join(REPLICA_SHARED_FILES_DIR, new_name)

                if not os.path.exists(old_path) or not os.path.isfile(old_path):
                    client_sock.sendall(b"ERROR FILE_NOT_FOUND\n")
                    continue

                if os.path.exists(new_path):
                    client_sock.sendall(b"ERROR NAME_CONFLICT\n")
                    continue

                try:
                    os.rename(old_path, new_path)
                    client_sock.sendall(
                        f"OK FILE_RENAMED {new_name}\n".encode('utf-8')
                    )
                    logger.info(
                        f"Renamed replicated file: '{old_name}' → "
                        f"'{new_name}'"
                    )
                except Exception as ren_err:
                    logger.error(
                        f"Rename failed from '{old_name}' to "
                        f"'{new_name}': {ren_err}"
                    )
                    client_sock.sendall(b"ERROR RENAME_FAILED\n")

            else:
                client_sock.sendall(b"ERROR UNKNOWN_COMMAND\n")
                logger.warning(
                    f"Unknown command received from {addr}: {cmd}"
                )

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
    # Ensure replica storage exists on startup
    os.makedirs(REPLICA_SHARED_FILES_DIR, exist_ok=True)

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    try:
        server.bind((REPLICA_SERVER_HOST, REPLICA_SERVER_PORT))
    except Exception as bind_err:
        logger.critical(
            f"Could not bind replica server to "
            f"{REPLICA_SERVER_HOST}:{REPLICA_SERVER_PORT}: {bind_err}"
        )
        return

    server.listen()
    logger.info(
        f"Replica TCP Server running on "
        f"{REPLICA_SERVER_HOST}:{REPLICA_SERVER_PORT}..."
    )

    try:
        while True:
            try:
                client_sock, addr = server.accept()
                thread = threading.Thread(
                    target=handle_client, args=(client_sock, addr)
                )
                thread.daemon = True
                thread.start()
            except Exception as loop_err:
                logger.error(f"Error in server accept loop: {loop_err}")
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received. Shutting down replica server...")
    finally:
        server.close()
        logger.info("Replica server socket closed.")


if __name__ == '__main__':
    main()
