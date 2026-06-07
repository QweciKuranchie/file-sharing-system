"""
Authentication and database module for the Distributed File-Sharing System.
Provides database initialization, user registration, user login, JWT management,
and storage quota tracking.
"""

import os
import sqlite3
import datetime
import hashlib
import jwt
import time
from datetime import timezone

DB_PATH = "fileshare.db"
JWT_SECRET = os.environ.get("JWT_SECRET", "super-secret-jwt-key")
JWT_ALGORITHM = "HS256"


def get_db_connection(db_path=DB_PATH):
    """
    Establishes a connection to the SQLite database.
    Enforces foreign key constraints and sets Row factory for name-based access.
    """
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(db_path=DB_PATH):
    """
    Initializes the SQLite database schema if the tables do not exist.
    Creates the 'users' and 'files' tables.
    """
    conn = get_db_connection(db_path)
    cursor = conn.cursor()
    
    # Create users table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        quota_limit_bytes INTEGER DEFAULT 52428800,  -- Default 50MB
        quota_used_bytes INTEGER DEFAULT 0,
        created_at TEXT NOT NULL
    );
    """)
    
    # Create files table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        filename TEXT NOT NULL,
        original_name TEXT NOT NULL,
        file_type TEXT NOT NULL,
        file_size_bytes INTEGER NOT NULL,
        uploaded_at TEXT NOT NULL,
        owner_id INTEGER NOT NULL,
        FOREIGN KEY (owner_id) REFERENCES users (id) ON DELETE CASCADE
    );
    """)
    
    conn.commit()
    conn.close()


def hash_password(password: str) -> str:
    """
    Generates a secure SHA-256 hash of a password using PBKDF2 with a random salt.
    Format returned is 'salt_hex:hash_hex'.
    """
    salt = os.urandom(16)
    hashed = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 100000)
    return f"{salt.hex()}:{hashed.hex()}"


def verify_password(stored_password_hash: str, password_to_verify: str) -> bool:
    """
    Verifies a password against the stored salt and hash format.
    """
    try:
        salt_hex, hashed_hex = stored_password_hash.split(":")
        salt = bytes.fromhex(salt_hex)
        hashed = bytes.fromhex(hashed_hex)
        test_hashed = hashlib.pbkdf2_hmac('sha256', password_to_verify.encode('utf-8'), salt, 100000)
        return test_hashed == hashed
    except (ValueError, TypeError, AttributeError):
        return False


def register_user(username: str, email: str, password: str, db_path=DB_PATH) -> int:
    """
    Registers a new user with uniqueness checks on username and email.
    Hashes the password before storage.
    Returns the user ID of the newly registered user.
    Raises ValueError if registration fails or duplicate details are provided.
    """
    if not username or not username.strip():
        raise ValueError("Username cannot be empty")
    if not email or not email.strip():
        raise ValueError("Email cannot be empty")
    if not password or not password.strip():
        raise ValueError("Password cannot be empty")
        
    username = username.strip()
    email = email.strip()
    
    init_db(db_path)
    conn = get_db_connection(db_path)
    cursor = conn.cursor()
    
    try:
        # Pre-check for duplicate username to raise user-friendly errors
        cursor.execute("SELECT id FROM users WHERE username = ?", (username,))
        if cursor.fetchone():
            raise ValueError("Username already exists")
            
        # Pre-check for duplicate email
        cursor.execute("SELECT id FROM users WHERE email = ?", (email,))
        if cursor.fetchone():
            raise ValueError("Email already exists")
            
        pwd_hash = hash_password(password)
        created_at = datetime.datetime.now(timezone.utc).isoformat()
        
        cursor.execute(
            "INSERT INTO users (username, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
            (username, email, pwd_hash, created_at)
        )
        user_id = cursor.lastrowid
        conn.commit()
        return user_id
    except sqlite3.IntegrityError as e:
        # Fallback security in case of race conditions
        err_msg = str(e).lower()
        if "username" in err_msg:
            raise ValueError("Username already exists")
        elif "email" in err_msg:
            raise ValueError("Email already exists")
        else:
            raise ValueError(f"Registration failed: {e}")
    finally:
        conn.close()


def login_user(username: str, password: str, db_path=DB_PATH) -> str:
    """
    Authenticates a user and returns a 30-minute JWT token if successful.
    Raises ValueError if credentials are invalid or missing.
    """
    if not username or not username.strip():
        raise ValueError("Username is required")
    if not password:
        raise ValueError("Password is required")
        
    username = username.strip()
    
    init_db(db_path)
    conn = get_db_connection(db_path)
    cursor = conn.cursor()
    
    cursor.execute("SELECT id, password_hash FROM users WHERE username = ?", (username,))
    row = cursor.fetchone()
    conn.close()
    
    if not row:
        raise ValueError("Invalid username or password")
        
    user_id = row["id"]
    stored_hash = row["password_hash"]
    
    if not verify_password(stored_hash, password):
        raise ValueError("Invalid username or password")
        
    # Generate token with 30 minute expiry
    payload = {
        "user_id": user_id,
        "exp": datetime.datetime.now(timezone.utc) + datetime.timedelta(minutes=30)
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    if isinstance(token, bytes):
        return token.decode('utf-8')
    return token


def validate_jwt(token: str) -> int:
    """
    Decodes and validates a JWT token.
    Returns the user_id if valid.
    Raises ValueError if token has expired or is invalid/tampered.
    """
    if not token:
        raise ValueError("Token is required")
        
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload["user_id"]
    except jwt.ExpiredSignatureError:
        raise ValueError("Token has expired")
    except jwt.InvalidTokenError:
        raise ValueError("Invalid token")


def check_quota(user_id: int, file_size_bytes: int, db_path=DB_PATH) -> bool:
    """
    Checks if adding the given file size would exceed the user's storage quota.
    Returns True if user has enough quota, False otherwise (or if user doesn't exist).
    """
    if file_size_bytes < 0:
        return False
        
    init_db(db_path)
    conn = get_db_connection(db_path)
    cursor = conn.cursor()
    
    cursor.execute("SELECT quota_used_bytes, quota_limit_bytes FROM users WHERE id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    
    if not row:
        return False
        
    quota_used = row["quota_used_bytes"]
    quota_limit = row["quota_limit_bytes"]
    
    return (quota_used + file_size_bytes) <= quota_limit


def update_quota(user_id: int, delta_bytes: int, db_path=DB_PATH) -> None:
    """
    Increments or decrements a user's used quota by the specified delta.
    Guards against underflow by rejecting updates that would make usage negative.
    Verifies that the target user exists and raises ValueError if not.
    """
    init_db(db_path)
    
    for attempt in range(5):
        try:
            conn = get_db_connection(db_path)
            cursor = conn.cursor()
            try:
                with conn:
                    conn.execute("BEGIN IMMEDIATE")
                    cursor.execute("SELECT quota_used_bytes FROM users WHERE id = ?", (user_id,))
                    row = cursor.fetchone()
                    if not row:
                        raise ValueError("User does not exist")
                    
                    current_quota = row["quota_used_bytes"]
                    new_quota = current_quota + delta_bytes
                    if new_quota < 0:
                        raise ValueError("Quota used bytes cannot be negative")
                        
                    cursor.execute(
                        "UPDATE users SET quota_used_bytes = ? WHERE id = ?",
                        (new_quota, user_id)
                    )
                    return
            finally:
                conn.close()
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < 4:
                time.sleep(0.05 * (attempt + 1))
                continue
            raise


def reserve_quota(user_id: int, file_size_bytes: int, db_path=DB_PATH) -> bool:
    """
    Atomically checks if adding the given file size would exceed the user's storage quota
    and, if not, increments the user's used quota by that size in a single transaction.
    Returns True if quota was successfully reserved/updated, False otherwise.
    Raises ValueError if user does not exist or if file_size_bytes is negative.
    """
    if file_size_bytes < 0:
        raise ValueError("File size cannot be negative")
        
    init_db(db_path)
    
    for attempt in range(5):
        try:
            conn = get_db_connection(db_path)
            cursor = conn.cursor()
            try:
                with conn:
                    conn.execute("BEGIN IMMEDIATE")
                    cursor.execute("SELECT quota_used_bytes, quota_limit_bytes FROM users WHERE id = ?", (user_id,))
                    row = cursor.fetchone()
                    if not row:
                        raise ValueError("User does not exist")
                        
                    quota_used = row["quota_used_bytes"]
                    quota_limit = row["quota_limit_bytes"]
                    
                    if (quota_used + file_size_bytes) > quota_limit:
                        return False
                        
                    cursor.execute(
                        "UPDATE users SET quota_used_bytes = quota_used_bytes + ? WHERE id = ?",
                        (file_size_bytes, user_id)
                    )
                    return True
            finally:
                conn.close()
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < 4:
                time.sleep(0.05 * (attempt + 1))
                continue
            raise


if __name__ == "__main__":
    init_db()
    print("Database initialized successfully.")
