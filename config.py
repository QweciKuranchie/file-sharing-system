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
REPLICA_SHARED_FILES_DIR = os.path.join(BASE_DIR, "shared_files_replica")

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

# ---------------------------------------------------------------------------
# TCP Authentication Settings (Layer 2 Security Boundary)
# ---------------------------------------------------------------------------
import sys
is_testing = ("pytest" in sys.modules or "unittest" in sys.modules or "pytest_current_test" in os.environ)
is_production = os.environ.get("ENV") == "production" or os.environ.get("FLASK_ENV") == "production"

TCP_CLIENT_SECRET = os.environ.get("TCP_CLIENT_SECRET")
TCP_REPLICATION_SECRET = os.environ.get("TCP_REPLICATION_SECRET")

if is_production:
    if not TCP_CLIENT_SECRET or not TCP_REPLICATION_SECRET:
        raise RuntimeError(
            "Explicit configuration of TCP_CLIENT_SECRET and TCP_REPLICATION_SECRET "
            "environment variables is required in production."
        )
else:
    # Defaults for development and test suite runs
    if not TCP_CLIENT_SECRET:
        TCP_CLIENT_SECRET = "default-test-client-secret-12345"
    if not TCP_REPLICATION_SECRET:
        TCP_REPLICATION_SECRET = "default-test-replication-secret-67890"

# Maintain backward compatibility for modules importing TCP_SHARED_SECRET
TCP_SHARED_SECRET = TCP_CLIENT_SECRET

