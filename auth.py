import sqlite3
import os
import time
import base64
import hmac
import hashlib
import json
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash

DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fileshare.db")
DB_PATH = DEFAULT_DB_PATH

def set_db_path(path):
    global DB_PATH
    DB_PATH = path

SECRET_KEY = os.environ.get("SECRET_KEY", "super-secret-key-change-in-prod")

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn

def init_db():
    """Initializes the SQLite database schemas for users and files."""
    conn = get_db_connection()
    try:
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    email TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    quota_limit_bytes INTEGER DEFAULT 52428800,
                    quota_used_bytes INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    filename TEXT NOT NULL,
                    original_name TEXT NOT NULL,
                    file_type TEXT NOT NULL,
                    file_size_bytes INTEGER NOT NULL,
                    uploaded_at TEXT NOT NULL,
                    owner_id INTEGER NOT NULL,
                    FOREIGN KEY(owner_id) REFERENCES users(id)
                );
            """)
    finally:
        conn.close()

# Token Helpers
def base64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode('utf-8').rstrip('=')

def base64url_decode(data: str) -> bytes:
    padding = '=' * (4 - (len(data) % 4))
    return base64.urlsafe_b64decode(data + padding)

def generate_token(user_id: int, secret: str = SECRET_KEY, expires_in: int = 1800) -> str:
    """Generates a secure JWT-like token for the user session with a 30-minute expiry."""
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "user_id": user_id,
        "exp": int(time.time()) + expires_in
    }
    header_b64 = base64url_encode(json.dumps(header).encode('utf-8'))
    payload_b64 = base64url_encode(json.dumps(payload).encode('utf-8'))
    signing_input = f"{header_b64}.{payload_b64}".encode('utf-8')
    signature = hmac.new(secret.encode('utf-8'), signing_input, hashlib.sha256).digest()
    signature_b64 = base64url_encode(signature)
    return f"{header_b64}.{payload_b64}.{signature_b64}"

def decode_token(token: str, secret: str = SECRET_KEY) -> dict:
    """Decodes and verifies a JWT-like token. Raises ValueError on failure/expiry."""
    try:
        parts = token.split('.')
        if len(parts) != 3:
            raise ValueError("Invalid token format")
        header_b64, payload_b64, signature_b64 = parts
        signing_input = f"{header_b64}.{payload_b64}".encode('utf-8')
        expected_signature = hmac.new(secret.encode('utf-8'), signing_input, hashlib.sha256).digest()
        expected_signature_b64 = base64url_encode(expected_signature)
        if not hmac.compare_digest(signature_b64, expected_signature_b64):
            raise ValueError("Signature verification failed")
        payload = json.loads(base64url_decode(payload_b64).decode('utf-8'))
        if payload.get("exp", 0) < time.time():
            raise ValueError("Token has expired")
        return payload
    except Exception as e:
        raise ValueError(f"Invalid token: {str(e)}")

# User Authentication Interface
def register_user(username, email, password):
    """Registers a new user. Returns user_id, or raises ValueError if validation/uniqueness fails."""
    if not username or not email or not password:
        raise ValueError("All fields (username, email, password) are required.")
    
    conn = get_db_connection()
    try:
        with conn:
            # Check unique constraints manually to raise descriptive errors
            cursor = conn.execute("SELECT 1 FROM users WHERE username = ?", (username,))
            if cursor.fetchone():
                raise ValueError("Username is already taken.")
            
            cursor = conn.execute("SELECT 1 FROM users WHERE email = ?", (email,))
            if cursor.fetchone():
                raise ValueError("Email is already registered.")
            
            password_hash = generate_password_hash(password)
            created_at = datetime.utcnow().isoformat()
            
            cursor = conn.execute(
                "INSERT INTO users (username, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
                (username, email, password_hash, created_at)
            )
            return cursor.lastrowid
    finally:
        conn.close()

def login_user(username_or_email, password):
    """Logs in a user. Returns a signed JWT token on success, raises ValueError on failure."""
    if not username_or_email or not password:
        raise ValueError("Username/email and password are required.")
    
    conn = get_db_connection()
    try:
        cursor = conn.execute(
            "SELECT id, password_hash FROM users WHERE username = ? OR email = ?",
            (username_or_email, username_or_email)
        )
        row = cursor.fetchone()
        if not row or not check_password_hash(row["password_hash"], password):
            raise ValueError("Invalid username/email or password.")
        
        return generate_token(row["id"])
    finally:
        conn.close()

def get_user_by_id(user_id):
    """Retrieves user details from database by ID."""
    conn = get_db_connection()
    try:
        cursor = conn.execute("SELECT id, username, email, quota_limit_bytes, quota_used_bytes FROM users WHERE id = ?", (user_id,))
        row = cursor.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

# Quota Management Interface
def check_quota(user_id: int, file_size: int) -> bool:
    """Checks if the user has enough quota for the given file size."""
    if file_size < 0:
        raise ValueError("File size cannot be negative.")
        
    conn = get_db_connection()
    try:
        cursor = conn.execute("SELECT quota_used_bytes, quota_limit_bytes FROM users WHERE id = ?", (user_id,))
        row = cursor.fetchone()
        if not row:
            raise ValueError(f"User with id {user_id} does not exist.")
        return row["quota_used_bytes"] + file_size <= row["quota_limit_bytes"]
    finally:
        conn.close()

def reserve_quota(user_id: int, file_size: int) -> bool:
    """
    Atomically checks if user has enough quota and, if so, increments usage.
    Returns True if quota was successfully reserved, False otherwise.
    """
    if file_size < 0:
        raise ValueError("File size cannot be negative.")
        
    conn = get_db_connection()
    # Lock database for writes immediately
    conn.execute("BEGIN IMMEDIATE")
    try:
        cursor = conn.execute("SELECT quota_used_bytes, quota_limit_bytes FROM users WHERE id = ?", (user_id,))
        row = cursor.fetchone()
        if not row:
            conn.rollback()
            raise ValueError(f"User with id {user_id} does not exist.")
            
        current_used = row["quota_used_bytes"]
        limit = row["quota_limit_bytes"]
        
        if current_used + file_size > limit:
            conn.rollback()
            return False
            
        new_used = current_used + file_size
        cursor = conn.execute("UPDATE users SET quota_used_bytes = ? WHERE id = ?", (new_used, user_id))
        if cursor.rowcount == 0:
            conn.rollback()
            raise ValueError(f"Failed to update user quota.")
            
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def update_quota(user_id: int, delta: int):
    """
    Increments or decrements quota usage.
    Guards against negative usage (underflow) and verifies user existence.
    """
    conn = get_db_connection()
    conn.execute("BEGIN IMMEDIATE")
    try:
        cursor = conn.execute("SELECT quota_used_bytes FROM users WHERE id = ?", (user_id,))
        row = cursor.fetchone()
        if not row:
            conn.rollback()
            raise ValueError(f"User with id {user_id} does not exist.")
            
        current_used = row["quota_used_bytes"]
        new_used = current_used + delta
        
        if new_used < 0:
            conn.rollback()
            raise ValueError(f"Quota update underflow: new quota_used_bytes would be {new_used} (less than 0).")
            
        cursor = conn.execute("UPDATE users SET quota_used_bytes = ? WHERE id = ?", (new_used, user_id))
        if cursor.rowcount == 0:
            conn.rollback()
            raise ValueError("Failed to update user quota.")
            
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

# Auto-initialize database on module import
init_db()
