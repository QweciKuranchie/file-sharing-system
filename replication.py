"""
replication.py — Replica propagation and sync operations.

This module implements actual socket communication with the Replica Server
to replicate uploads, propagate renames, and propagate deletions from the
Primary Server.
"""

import socket
import os
import logging
from typing import Optional
from enum import Enum

from config import REPLICA_SERVER_HOST, REPLICA_SERVER_PORT

# Configure logging
logger = logging.getLogger("replication")

# Timeouts (seconds)
_FILE_TRANSFER_TIMEOUT = 30
_METADATA_COMMAND_TIMEOUT = 10


class ReplicationOutcome(Enum):
    SUCCESS = 1
    REJECTED = 2
    AMBIGUOUS = 3



def _read_line(sock: socket.socket) -> str:
    """Read a single newline-terminated line from a socket.

    Returns the decoded line with trailing whitespace stripped.
    Returns an empty string if the connection closes before a full line.
    """
    buf = bytearray()
    while True:
        data = sock.recv(1)
        if not data:
            break
        buf.extend(data)
        if data == b'\n':
            break
    return buf.decode('utf-8').strip()


def _connect(timeout: float) -> Optional[socket.socket]:
    """Create a TCP connection to the replica server.

    Returns the connected socket, or None if the connection fails.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((REPLICA_SERVER_HOST, REPLICA_SERVER_PORT))
        from config import TCP_REPLICATION_SECRET
        auth_cmd = f"AUTH {TCP_REPLICATION_SECRET}\n"
        sock.sendall(auth_cmd.encode('utf-8'))
        auth_resp = _read_line(sock)
        if auth_resp != "OK AUTHENTICATED":
            logger.error(f"Authentication with replica failed: {auth_resp}")
            sock.close()
            return None
        return sock
    except (ConnectionRefusedError, socket.timeout, OSError) as err:
        logger.error(
            f"Cannot connect to replica at "
            f"{REPLICA_SERVER_HOST}:{REPLICA_SERVER_PORT}: {err}"
        )
        sock.close()
        return None


def replicate_file(filename: str, filepath: str) -> bool:
    """Replicate a newly uploaded file to the Replica Server.

    Opens a TCP connection to the replica, sends
    ``REPLICATE <filename> <size>\\n``, waits for ``READY``, streams the
    file bytes, and blocks until ``OK REPLICATED``.

    Parameters
    ----------
    filename : str
        The name under which the file is stored.
    filepath : str
        Absolute path to the file on the primary's local disk.

    Returns
    -------
    bool
        True if the file was replicated successfully, False otherwise.
    """
    # Read file from disk
    try:
        file_size = os.path.getsize(filepath)
    except OSError as err:
        logger.error(f"Cannot stat file for replication '{filepath}': {err}")
        return False

    sock = _connect(_FILE_TRANSFER_TIMEOUT)
    if sock is None:
        return False

    try:
        # Send the REPLICATE command
        command = f"REPLICATE {filename} {file_size}\n"
        sock.sendall(command.encode('utf-8'))

        # Wait for READY
        response = _read_line(sock)
        if response != "READY":
            logger.error(
                f"Replica did not send READY for '{filename}': {response}"
            )
            return False

        # Stream file bytes
        with open(filepath, 'rb') as f:
            while True:
                chunk = f.read(4096)
                if not chunk:
                    break
                sock.sendall(chunk)

        # Keep a bounded timeout for the final acknowledgement instead of switching to blocking mode
        sock.settimeout(_METADATA_COMMAND_TIMEOUT)

        # Wait for final acknowledgement
        final = _read_line(sock)
        if final == "OK REPLICATED":
            logger.info(
                f"File replicated successfully: '{filename}' "
                f"({file_size} bytes)"
            )
            return True
        else:
            logger.error(
                f"Replication failed for '{filename}': replica responded "
                f"'{final}'"
            )
            return False

    except (socket.timeout, OSError) as err:
        logger.error(f"Replication error for '{filename}': {err}")
        return False
    finally:
        try:
            sock.close()
        except OSError:
            pass


def propagate_delete(filename: str) -> ReplicationOutcome:
    """Propagate the deletion of a file to the Replica Server.

    Sends ``DELETE <filename>\\n`` and expects ``OK FILE_DELETED``.

    Parameters
    ----------
    filename : str
        The name of the file that was deleted on the primary.

    Returns
    -------
    ReplicationOutcome
        The structured outcome of the deletion propagation.
    """
    sock = _connect(_METADATA_COMMAND_TIMEOUT)
    if sock is None:
        return ReplicationOutcome.AMBIGUOUS

    try:
        command = f"DELETE {filename}\n"
        sock.sendall(command.encode('utf-8'))

        response = _read_line(sock)
        if response == "OK FILE_DELETED":
            logger.info(f"Deletion propagated to replica: '{filename}'")
            return ReplicationOutcome.SUCCESS
        elif response.startswith("ERROR"):
            logger.error(
                f"Replica explicitly rejected delete for '{filename}': {response}"
            )
            return ReplicationOutcome.REJECTED
        else:
            logger.error(
                f"Replica delete failed with ambiguous response for '{filename}': {response}"
            )
            return ReplicationOutcome.AMBIGUOUS

    except (socket.timeout, OSError) as err:
        logger.error(f"Propagate delete error for '{filename}': {err}")
        return ReplicationOutcome.AMBIGUOUS
    finally:
        try:
            sock.close()
        except OSError:
            pass


def propagate_rename(old_filename: str, new_filename: str) -> ReplicationOutcome:
    """Propagate a file rename operation to the Replica Server.

    Sends ``RENAME <old> <new>\\n`` and expects a response starting with
    ``OK FILE_RENAMED``.

    Parameters
    ----------
    old_filename : str
        The original name of the file.
    new_filename : str
        The new name of the file.

    Returns
    -------
    ReplicationOutcome
        The structured outcome of the rename propagation.
    """
    sock = _connect(_METADATA_COMMAND_TIMEOUT)
    if sock is None:
        return ReplicationOutcome.AMBIGUOUS

    try:
        command = f"RENAME {old_filename} {new_filename}\n"
        sock.sendall(command.encode('utf-8'))

        response = _read_line(sock)
        if response.startswith("OK FILE_RENAMED"):
            logger.info(
                f"Rename propagated to replica: '{old_filename}' → "
                f"'{new_filename}'"
            )
            return ReplicationOutcome.SUCCESS
        elif response.startswith("ERROR"):
            logger.error(
                f"Replica rename explicitly rejected '{old_filename}' → "
                f"'{new_filename}': {response}"
            )
            return ReplicationOutcome.REJECTED
        else:
            logger.error(
                f"Replica rename failed with ambiguous response '{old_filename}' → "
                f"'{new_filename}': {response}"
            )
            return ReplicationOutcome.AMBIGUOUS

    except (socket.timeout, OSError) as err:
        logger.error(
            f"Propagate rename error '{old_filename}' → "
            f"'{new_filename}': {err}"
        )
        return ReplicationOutcome.AMBIGUOUS
    finally:
        try:
            sock.close()
        except OSError:
            pass
