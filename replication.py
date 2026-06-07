"""
Replication module for the Distributed File-Sharing System.
Propagates uploads, deletes, and renames from the primary server to the replica server.
"""

import os
import socket
import datetime

REPLICA_HOST = os.environ.get("REPLICA_SERVER_HOST", "127.0.0.1")
REPLICA_PORT = int(os.environ.get("REPLICA_SERVER_PORT", 9001))


def log_event(message):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [REPLICATION] {message}")


def replicate_file(filename: str, filepath: str) -> bool:
    """
    Replicates a file from primary to replica.
    Opens a fresh TCP connection to port 9001, sends REPLICATE <filename> <size>,
    waits for READY, sends file bytes, blocks until OK REPLICATED.
    """
    try:
        if not os.path.exists(filepath):
            log_event(f"Error: File to replicate does not exist: {filepath}")
            return False
            
        size = os.path.getsize(filepath)
        
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(10.0)  # 10s timeout for replication operations
            sock.connect((REPLICA_HOST, REPLICA_PORT))
            
            # Send REPLICATE command
            command = f"REPLICATE {filename} {size}\n"
            sock.sendall(command.encode('utf-8'))
            
            # Read line for response
            line_bytes = bytearray()
            while True:
                b = sock.recv(1)
                if not b:
                    break
                if b == b'\n':
                    break
                if b != b'\r':
                    line_bytes.extend(b)
            response = line_bytes.decode('utf-8').strip()
            
            if response != "READY":
                log_event(f"Failed to replicate {filename}: replica returned {response}")
                return False
                
            # Send file bytes
            with open(filepath, 'rb') as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    sock.sendall(chunk)
                    
            # Read OK REPLICATED response
            line_bytes = bytearray()
            while True:
                b = sock.recv(1)
                if not b:
                    break
                if b == b'\n':
                    break
                if b != b'\r':
                    line_bytes.extend(b)
            ack = line_bytes.decode('utf-8').strip()
            
            if ack == "OK REPLICATED":
                log_event(f"Successfully replicated {filename} ({size} bytes) to replica.")
                return True
            else:
                log_event(f"Replication failed for {filename}: replica returned {ack}")
                return False
                
    except (socket.timeout, ConnectionRefusedError, socket.error) as e:
        log_event(f"Replication connection refused or timed out for {filename}: {e}")
        return False
    except Exception as e:
        log_event(f"Replication error for {filename}: {e}")
        return False


def propagate_delete(filename: str) -> bool:
    """
    Propagates a delete operation to the replica server.
    Sends DELETE <filename>.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(10.0)
            sock.connect((REPLICA_HOST, REPLICA_PORT))
            
            command = f"DELETE {filename}\n"
            sock.sendall(command.encode('utf-8'))
            
            # Read response
            line_bytes = bytearray()
            while True:
                b = sock.recv(1)
                if not b:
                    break
                if b == b'\n':
                    break
                if b != b'\r':
                    line_bytes.extend(b)
            response = line_bytes.decode('utf-8').strip()
            
            if response == "OK FILE_DELETED":
                log_event(f"Propagated delete for {filename} to replica.")
                return True
            else:
                log_event(f"Delete propagation failed for {filename}: replica returned {response}")
                return False
                
    except (socket.timeout, ConnectionRefusedError, socket.error) as e:
        log_event(f"Delete propagation connection refused or timed out for {filename}: {e}")
        return False
    except Exception as e:
        log_event(f"Delete propagation error for {filename}: {e}")
        return False


def quote_filename(name: str) -> str:
    """
    Quotes a filename by escaping backslashes and double quotes, and wrapping in double quotes.
    """
    escaped = name.replace('\\', '\\\\').replace('"', '\\"')
    return f'"{escaped}"'


def propagate_rename(old_name: str, new_name: str) -> bool:
    """
    Propagates a rename operation to the replica server.
    Sends RENAME <old_name> <new_name>.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(10.0)
            sock.connect((REPLICA_HOST, REPLICA_PORT))
            
            command = f"RENAME {quote_filename(old_name)} {quote_filename(new_name)}\n"
            sock.sendall(command.encode('utf-8'))
            
            # Read response
            line_bytes = bytearray()
            while True:
                b = sock.recv(1)
                if not b:
                    break
                if b == b'\n':
                    break
                if b != b'\r':
                    line_bytes.extend(b)
            response = line_bytes.decode('utf-8').strip()
            
            if response == f"OK FILE_RENAMED {new_name}":
                log_event(f"Propagated rename of {old_name} to {new_name} to replica.")
                return True
            else:
                log_event(f"Rename propagation failed for {old_name} to {new_name}: replica returned {response}")
                return False
                
    except (socket.timeout, ConnectionRefusedError, socket.error) as e:
        log_event(f"Rename propagation connection refused or timed out for {old_name}: {e}")
        return False
    except Exception as e:
        log_event(f"Rename propagation error for {old_name}: {e}")
        return False
