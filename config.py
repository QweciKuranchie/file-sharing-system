"""
config.py — Centralised application configuration.

All tuneable constants live here so that every module imports from one place.
"""

import os
import secrets

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE_PATH = os.path.join(BASE_DIR, "fileshare.db")
SHARED_FILES_DIR = os.path.join(BASE_DIR, "shared_files")

# ---------------------------------------------------------------------------
# JWT Settings
# ---------------------------------------------------------------------------
# In production the secret should come from an environment variable.
# For the LAN demo we fall back to a random-per-process secret so that
# tokens cannot be forged even if the source code is visible.
JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY", secrets.token_hex(32))
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_MINUTES = 30

# ---------------------------------------------------------------------------
# Quota & Upload Limits
# ---------------------------------------------------------------------------
DEFAULT_QUOTA_BYTES = 52_428_800   # 50 MB per user
MAX_FILE_SIZE_BYTES = 10_485_760   # 10 MB per upload

# ---------------------------------------------------------------------------
# Server Network Addresses (TCP layer)
# ---------------------------------------------------------------------------
PRIMARY_SERVER_HOST = os.environ.get("PRIMARY_HOST", "127.0.0.1")
PRIMARY_SERVER_PORT = int(os.environ.get("PRIMARY_PORT", 9000))

REPLICA_SERVER_HOST = os.environ.get("REPLICA_HOST", "127.0.0.1")
REPLICA_SERVER_PORT = int(os.environ.get("REPLICA_PORT", 9001))

FLASK_PORT = int(os.environ.get("FLASK_PORT", 5000))
