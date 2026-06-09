"""
replication.py — Skeleton for replica propagation and sync operations.

In BE-3, this module will implement actual socket communication with the
Replica Server to replicate uploads, renames, and deletions. For now, it
behaves as a skeleton that logs operations and returns success.
"""

import logging

logger = logging.getLogger("replication")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def replicate_file(filename: str, file_size: int) -> bool:
    """Trigger replication of a newly uploaded file to the Replica Server.

    Parameters
    ----------
    filename : str
        The name of the file saved on the Primary Server.
    file_size : int
        The size of the file in bytes.

    Returns
    -------
    bool
        True if the file was replicated successfully, False otherwise.
    """
    logger.info(f"Replicating file '{filename}' ({file_size} bytes) to replica...")
    return True


def propagate_delete(filename: str) -> bool:
    """Propagate the deletion of a file to the Replica Server.

    Parameters
    ----------
    filename : str
        The name of the file that was deleted.

    Returns
    -------
    bool
        True if deletion was successfully propagated, False otherwise.
    """
    logger.info(f"Propagating deletion of '{filename}' to replica...")
    return True


def propagate_rename(old_filename: str, new_filename: str) -> bool:
    """Propagate a file rename operation to the Replica Server.

    Parameters
    ----------
    old_filename : str
        The original name of the file.
    new_filename : str
        The new name of the file.

    Returns
    -------
    bool
        True if rename was successfully propagated, False otherwise.
    """
    logger.info(f"Propagating rename of '{old_filename}' to '{new_filename}' to replica...")
    return True
