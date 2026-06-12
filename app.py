from flask import Flask, render_template, request, redirect, url_for, session, flash, g, jsonify, Response
import os
import datetime
from functools import wraps
import socket
import auth
from database import init_db

PRIMARY_HOST = os.environ.get("PRIMARY_HOST", "127.0.0.1")
PRIMARY_PORT = int(os.environ.get("PRIMARY_PORT", 9000))
REPLICA_HOST = os.environ.get("REPLICA_HOST", "127.0.0.1")
REPLICA_PORT = int(os.environ.get("REPLICA_PORT", 9001))

PRIMARY_DOWN = False


import sys
app = Flask(__name__)

# Enforce SECRET_KEY security boundary
secret_key = os.environ.get("SECRET_KEY")
if not secret_key:
    is_testing = ("pytest" in sys.modules or "unittest" in sys.modules or "pytest_current_test" in os.environ)
    is_production = os.environ.get("ENV") == "production" or os.environ.get("FLASK_ENV") == "production"
    if is_testing:
        secret_key = "test-secret-key-12345"
    elif not is_production:
        secret_key = "dev-secret-key-67890"
    else:
        raise RuntimeError("SECRET_KEY environment variable is required but not set in production.")
app.secret_key = secret_key

# Set explicit session cookie security options
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "false").lower() in ("true", "1")
)


# Ensure database tables exist on startup
init_db()

# Configure sessions to expire in 30 minutes of inactivity
app.permanent_session_lifetime = datetime.timedelta(minutes=30)

# JWT session middleware decorator
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if g.user is None:
            # Unauthenticated or expired session redirects silently to /login
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# TCP Socket Protocol Helpers
def read_line(sock):
    """Read one text line from a socket, byte by byte. Safe for control lines only.
    Do NOT use for sockets that will subsequently stream binary data —
    use read_line_buffered() instead so no binary bytes are consumed."""
    line = bytearray()
    while True:
        c = sock.recv(1)
        if not c or not isinstance(c, (bytes, bytearray)):
            break
        line.extend(c)
        if c == b'\n':
            break
    return line.decode('utf-8').strip()

def read_line_buffered(sock, buf_size=4096):
    """Read one text line from a socket using a read buffer.
    Returns (line_str, leftover_bytes) where leftover_bytes is any data
    already received from the socket that comes after the newline.
    Use this before streaming binary data to avoid losing the first bytes."""
    buffer = bytearray()
    while b'\n' not in buffer:
        chunk = sock.recv(buf_size)
        if not chunk:
            break
        buffer.extend(chunk)
    if b'\n' in buffer:
        line_bytes, leftover = buffer.split(b'\n', 1)
        return line_bytes.decode('utf-8').strip(), bytes(leftover)
    return buffer.decode('utf-8', errors='replace').strip(), b''

def connect_and_authenticate(host, port, timeout=5):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if timeout is not None:
        s.settimeout(timeout)
    try:
        s.connect((host, port))
        from config import TCP_CLIENT_SECRET
        s.sendall(f"AUTH {TCP_CLIENT_SECRET}\n".encode('utf-8'))
        auth_resp = read_line(s)
        if auth_resp != "OK AUTHENTICATED":
            raise PermissionError(f"Authentication failed: {auth_resp}")
        return s
    except Exception as e:
        try:
            s.close()
        except OSError:
            pass
        raise e

def ping_server(host, port, timeout=5):
    s = None
    try:
        s = connect_and_authenticate(host, port, timeout)
        s.sendall(b"PING\n")
        resp = read_line(s)
        return resp == "OK PONG"
    except Exception:
        return False
    finally:
        if s is not None:
            try:
                s.close()
            except OSError:
                pass

def send_tcp_command(host, port, command_str, file_data=None, timeout=5):
    s = None
    try:
        s = connect_and_authenticate(host, port, timeout)
    except PermissionError:
        return "ERROR UNAUTHORIZED"
    except Exception:
        return "ERROR CONNECTION_FAILED"
        
    try:
        if not command_str.endswith('\n'):
            command_str += '\n'
        s.sendall(command_str.encode('utf-8'))
        
        if file_data is not None:
            # UPLOAD handshake
            ready_resp = read_line(s)
            if ready_resp == "READY":
                s.sendall(file_data)
                s.settimeout(None)
                final_resp = read_line(s)
                return final_resp
            else:
                return ready_resp
        else:
            resp = read_line(s)
            return resp
    finally:
        if s is not None:
            try:
                s.close()
            except OSError:
                pass

def format_size(num_bytes):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if num_bytes < 1024.0:
            if unit == 'B':
                return f"{int(num_bytes)} {unit}"
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} TB"

app.jinja_env.filters['format_size'] = format_size

def get_file_type(filename):
    _, ext = os.path.splitext(filename.lower())
    if ext == '.pdf':
        return 'pdf'
    elif ext in ['.jpg', '.jpeg', '.png', '.gif']:
        return 'image'
    elif ext in ['.mp4', '.avi', '.mov']:
        return 'video'
    elif ext in ['.txt', '.docx', '.doc']:
        return 'document'
    else:
        return 'other'

import time

@app.before_request
def check_primary_health():
    global PRIMARY_DOWN
    # Only run health checks for non-static endpoints
    if not request.endpoint or request.endpoint == 'static':
        g.primary_down = PRIMARY_DOWN or session.get('primary_down', False)
        return
    PRIMARY_DOWN = not ping_server(PRIMARY_HOST, PRIMARY_PORT, timeout=5.0)
    session['primary_down'] = PRIMARY_DOWN
    g.primary_down = PRIMARY_DOWN

@app.after_request
def add_failover_header(response):
    try:
        is_down = getattr(g, 'primary_down', PRIMARY_DOWN or session.get('primary_down', False))
    except Exception:
        is_down = PRIMARY_DOWN
    if response.headers.get('X-Primary-Down') == 'true' or response.headers.get('X-Failover-Triggered') == 'true':
        is_down = True
    response.headers['X-Primary-Down'] = 'true' if is_down else 'false'
    if is_down:
        response.headers['X-Failover-Triggered'] = 'true'
    response.set_cookie('primary_down', 'true' if is_down else 'false', path='/', samesite='Lax')
    return response


@app.before_request
def load_user():
    """Runs before every request. Populates g.user from JWT token in session, or clears invalid sessions."""
    token = session.get('token')
    g.user = None
    if token:
        try:
            # Decode and validate the JWT token
            payload = auth.decode_token(token)
            g.user = auth.get_user_by_id(payload['user_id'])
            if not g.user:
                session.pop('token', None)
            else:
                # Refresh token on active requests
                new_token = auth.refresh_token(token)
                session['token'] = new_token
        except auth.TokenError:
            # If the JWT has expired or is invalid, clear it silently from session
            session.pop('token', None)


@app.route('/')
def index():
    if g.user:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if g.user:
        return redirect(url_for('dashboard'))
        
    if request.method == 'POST':
        username_or_email = request.form.get('username')
        password = request.form.get('password')

        if not username_or_email or not password:
            flash("Username and password are required.", "error")
            return render_template('login.html', active_page='login'), 400

        try:
            token = auth.login_user(username_or_email, password)
            session.permanent = True  # Enforce permanent session lifetime of 30 mins
            session['token'] = token
            return redirect(url_for('dashboard'))
        except auth.InvalidCredentialsError as e:
            flash(str(e), "error")
            return render_template('login.html', active_page='login'), 200
        except ValueError as e:
            flash(str(e), "error")
            return render_template('login.html', active_page='login'), 400
            
    return render_template('login.html', active_page='login')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if g.user:
        return redirect(url_for('dashboard'))
        
    if request.method == 'POST':
        username = request.form.get('username')
        email = request.form.get('email')
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')

        if not username or not email or not password:
            flash("All fields are required.", "error")
            return render_template('register.html', active_page='register'), 400

        if password != confirm_password:
            flash("Passwords do not match.", "error")
            return render_template('register.html', active_page='register'), 400
            
        try:
            auth.register_user(username, email, password)
            flash("Account created. Please log in.", "success")
            return redirect(url_for('login'))
        except auth.DuplicateUsernameError as e:
            flash(str(e), "error")
            return render_template('register.html', active_page='register'), 409
        except auth.DuplicateEmailError as e:
            flash(str(e), "error")
            return render_template('register.html', active_page='register'), 409
        except ValueError as e:
            flash(str(e), "error")
            return render_template('register.html', active_page='register'), 400
            
    return render_template('register.html', active_page='register')

@app.route('/logout')
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    search = request.args.get('search', '').strip()
    file_type = request.args.get('type', '').strip()
    files = auth.get_filtered_files(search=search, file_type=file_type)
    return render_template('dashboard.html', active_page='dashboard', files=files)

@app.route('/upload', methods=['POST'])
@login_required
def upload_file():
    if g.primary_down:
        return jsonify({
            "status": "error", 
            "message": "Primary server is offline. Write operations are unavailable in failover mode."
        }), 503

    if 'file' not in request.files:
        return jsonify({"status": "error", "message": "No file selected"}), 400
        
    file = request.files['file']
    if file.filename == '':
        return jsonify({"status": "error", "message": "No file selected"}), 400
        
    original_name = file.filename
    if not original_name:
        return jsonify({"status": "error", "message": "No file selected"}), 400
    
    # Block path traversal and null bytes; allow all other common filename characters.
    # original_name is only stored as a display label — safe_filename (below) is what
    # actually touches the filesystem and is strictly validated separately.
    import re
    if ('\x00' in original_name or
        '..' in original_name or
        '/' in original_name or
        '\\' in original_name or
        not re.match(r'^[a-zA-Z0-9_\-\.\(\)\[\]\{\} ,+!@#&\'\"=~$%^]+$', original_name)):
        return jsonify({"status": "error", "message": "Invalid filename characters"}), 400
        
    # Build a safe filename for disk/TCP: replace every unsafe character with '_',
    # collapse runs of underscores, and strip leading/trailing underscores or hyphens.
    # The original_name is preserved as the display label in the DB.
    _stem, _ext = os.path.splitext(os.path.basename(original_name))
    _ext = re.sub(r'[^a-zA-Z0-9]', '', _ext)          # keep only alnum in extension
    _stem = re.sub(r'[^a-zA-Z0-9_\-]', '_', _stem)    # replace unsafe chars in stem
    _stem = re.sub(r'_+', '_', _stem).strip('_-')      # collapse/strip underscores
    safe_filename = (_stem + ('.' + _ext if _ext else '')) if _stem else ''

    if (not safe_filename or
        safe_filename in ('.', '..') or
        '..' in safe_filename or
        safe_filename.startswith('.') or
        safe_filename.startswith('-') or
        safe_filename.endswith('.') or
        safe_filename.endswith('-') or
        not re.match(r'^[a-zA-Z0-9_\-\.]+$', safe_filename)):
        return jsonify({"status": "error", "message": "Invalid filename"}), 400
        
    file_data = file.read()
    file_size = len(file_data)
    
    if file_size > 10 * 1024 * 1024:
        return jsonify({"status": "error", "message": "File exceeds the 10MB limit."}), 413
        
    if not auth.reserve_quota(g.user['id'], file_size):
        return jsonify({"status": "error", "message": "Storage quota exceeded. Delete files to free space."}), 403

    saved_on_server = False
    saved_name = None
    try:
        response = send_tcp_command(PRIMARY_HOST, PRIMARY_PORT, f"UPLOAD {safe_filename} {file_size}", file_data)
        
        if response.startswith("OK FILE_SAVED"):
            parts = response.split(None, 2)
            saved_name = parts[2] if len(parts) > 2 else safe_filename
            saved_on_server = True
            
            file_type = get_file_type(saved_name)
            auth.add_file(saved_name, original_name, file_type, file_size, g.user['id'])
            
            if saved_name != original_name:
                msg = f"File uploaded and replicated successfully — saved as {saved_name}"
            else:
                msg = "File uploaded and replicated successfully."
            flash(msg, "success")
            
            return jsonify({
                "status": "success", 
                "message": msg, 
                "filename": saved_name
            }), 200
            
        elif "REPLICATION_FAILED" in response:
            parts = response.split(None, 2)
            saved_name = parts[2] if len(parts) > 2 else safe_filename
            saved_on_server = True
            
            file_type = get_file_type(saved_name)
            auth.add_file(saved_name, original_name, file_type, file_size, g.user['id'])
            
            msg = "File uploaded but replication failed. Replica may be out of sync."
            flash(msg, "warning")
            
            return jsonify({
                "status": "warning", 
                "message": msg,
                "filename": saved_name
            }), 207
            
        elif "INVALID_FILENAME" in response:
            auth.release_quota(g.user['id'], file_size)
            return jsonify({
                "status": "error",
                "message": "Invalid filename"
            }), 400
            
        elif "INVALID_COMMAND" in response:
            auth.release_quota(g.user['id'], file_size)
            return jsonify({
                "status": "error",
                "message": "Invalid command"
            }), 400
            
        else:
            auth.release_quota(g.user['id'], file_size)
            return jsonify({
                "status": "error", 
                "message": f"Upload failed: {response}"
            }), 500
            
    except Exception as e:
        if saved_on_server and saved_name:
            # Attempt compensating DELETE to clean up the orphaned file
            delete_succeeded = False
            try:
                del_response = send_tcp_command(PRIMARY_HOST, PRIMARY_PORT, f"DELETE {saved_name}")
                if del_response and "FILE_DELETED" in del_response:
                    delete_succeeded = True
            except Exception:
                pass

            if delete_succeeded:
                # File removed from disk — safe to release the quota reservation
                auth.release_quota(g.user['id'], file_size)
            else:
                # File still exists on disk — retain quota to keep accounting accurate
                import logging
                logging.getLogger("app").critical(
                    f"Compensating DELETE failed for '{saved_name}' "
                    f"(user {g.user['id']}). Quota retained for "
                    f"{file_size} bytes — manual reconciliation required."
                )
        else:
            # File was never saved on the server — safe to release quota
            auth.release_quota(g.user['id'], file_size)
        return jsonify({
            "status": "error", 
            "message": "Upload failed. Please try again."
        }), 500

@app.route('/download/<filename>')
@login_required
def download_file(filename):
    file_record = auth.get_file_by_name(filename)
    if not file_record:
        return jsonify({"status": "error", "message": "File not found on any server"}), 404
        
    host = REPLICA_HOST if g.primary_down else PRIMARY_HOST
    port = REPLICA_PORT if g.primary_down else PRIMARY_PORT
    
    failover_triggered = False
    s = None
    leftover = b''
    try:
        s = connect_and_authenticate(host, port, timeout=5)
        s.sendall(f"DOWNLOAD {filename}\n".encode('utf-8'))
        
        resp_line, leftover = read_line_buffered(s)
        if not resp_line:
            raise socket.error("Header read failure: empty response")
            
        if not resp_line.startswith("OK "):
            s.close()
            if "FILE_NOT_FOUND" in resp_line:
                return jsonify({"status": "error", "message": "File not found on any server"}), 404
            if "INVALID_FILENAME" in resp_line:
                return jsonify({"status": "error", "message": "Invalid filename"}), 400
            if "INVALID_COMMAND" in resp_line:
                return jsonify({"status": "error", "message": "Invalid command"}), 400
            return jsonify({"status": "error", "message": f"Download failed: {resp_line}"}), 500
            
        parts = resp_line.split()
        file_size = int(parts[1])
        s.settimeout(None)
        
    except Exception as e:
        if s:
            try:
                s.close()
            except Exception:
                pass
        
        if host == PRIMARY_HOST:
            g.primary_down = True
            global PRIMARY_DOWN
            PRIMARY_DOWN = True
            session['primary_down'] = True
            failover_triggered = True
            
            host = REPLICA_HOST
            port = REPLICA_PORT
            s = None
            try:
                s = connect_and_authenticate(host, port, timeout=5)
                s.sendall(f"DOWNLOAD {filename}\n".encode('utf-8'))
                
                resp_line, leftover = read_line_buffered(s)
                if not resp_line:
                    raise socket.error("Header read failure: empty response")
                    
                if not resp_line.startswith("OK "):
                    s.close()
                    if "FILE_NOT_FOUND" in resp_line:
                        response = jsonify({"status": "error", "message": "File not found on any server"})
                        response.headers['X-Primary-Down'] = 'true'
                        response.headers['X-Failover-Triggered'] = 'true'
                        response.set_cookie('primary_down', 'true', path='/', samesite='Lax')
                        return response, 404
                    if "INVALID_FILENAME" in resp_line:
                        response = jsonify({"status": "error", "message": "Invalid filename"})
                        response.headers['X-Primary-Down'] = 'true'
                        response.headers['X-Failover-Triggered'] = 'true'
                        response.set_cookie('primary_down', 'true', path='/', samesite='Lax')
                        return response, 400
                    if "INVALID_COMMAND" in resp_line:
                        response = jsonify({"status": "error", "message": "Invalid command"})
                        response.headers['X-Primary-Down'] = 'true'
                        response.headers['X-Failover-Triggered'] = 'true'
                        response.set_cookie('primary_down', 'true', path='/', samesite='Lax')
                        return response, 400
                    response = jsonify({"status": "error", "message": f"Download failed: {resp_line}"})
                    response.headers['X-Primary-Down'] = 'true'
                    response.headers['X-Failover-Triggered'] = 'true'
                    response.set_cookie('primary_down', 'true', path='/', samesite='Lax')
                    return response, 500
                    
                parts = resp_line.split()
                file_size = int(parts[1])
                s.settimeout(None)
            except Exception as replica_e:
                if s:
                    try:
                        s.close()
                    except Exception:
                        pass
                response = jsonify({"status": "error", "message": "Failed to connect to file server."})
                response.headers['X-Primary-Down'] = 'true'
                response.headers['X-Failover-Triggered'] = 'true'
                response.set_cookie('primary_down', 'true', path='/', samesite='Lax')
                return response, 500
        else:
            response = jsonify({"status": "error", "message": "Failed to connect to file server."})
            if g.primary_down:
                response.headers['X-Primary-Down'] = 'true'
                response.headers['X-Failover-Triggered'] = 'true'
                response.set_cookie('primary_down', 'true', path='/', samesite='Lax')
            return response, 500
        
    def generate_bytes(sock, size, initial_bytes=b''):
        """Stream exactly 'size' bytes: first flush any buffered bytes from the
        header read, then read the rest directly from the socket."""
        try:
            bytes_left = size
            # Flush bytes already buffered during header read
            if initial_bytes:
                to_send = initial_bytes[:bytes_left]
                yield to_send
                bytes_left -= len(to_send)
            # Stream remaining bytes from socket
            chunk_size = 65536
            while bytes_left > 0:
                to_read = min(chunk_size, bytes_left)
                chunk = sock.recv(to_read)
                if not chunk:
                    break
                yield chunk
                bytes_left -= len(chunk)
        finally:
            sock.close()
            
    import mimetypes
    mime_type, _ = mimetypes.guess_type(file_record['original_name'])
    if not mime_type:
        mime_type = 'application/octet-stream'
        
    # Build the display filename for Content-Disposition.
    # If the user renamed to something without an extension, fall back to the
    # stored filename's extension so the browser can open the file correctly.
    display_name = file_record['original_name']
    _, disp_ext = os.path.splitext(display_name)
    _, stored_ext = os.path.splitext(file_record['filename'])
    if stored_ext and not disp_ext:
        display_name = display_name + stored_ext
    # Escape any double-quotes in the filename to keep the header valid
    safe_display_name = display_name.replace('"', '\\"')

    headers = {
        'Content-Type': mime_type,
        'Content-Disposition': f'attachment; filename="{safe_display_name}"',
        'Content-Length': str(file_size)
    }
    if failover_triggered or g.primary_down:
        headers['X-Primary-Down'] = 'true'
        headers['X-Failover-Triggered'] = 'true'
        
    response = Response(generate_bytes(s, file_size, leftover), headers=headers)
    if failover_triggered or g.primary_down:
        response.set_cookie('primary_down', 'true', path='/', samesite='Lax')
    return response

@app.route('/delete/<filename>', methods=['POST'])
@login_required
def delete_file(filename):
    if g.primary_down:
        return jsonify({
            "status": "error", 
            "message": "Primary server is offline. Write operations are unavailable in failover mode."
        }), 503

    file_record = auth.get_file_by_name(filename)
    if not file_record:
        return jsonify({"status": "error", "message": "File not found"}), 404
        
    if file_record['owner_id'] != g.user['id']:
        return jsonify({"status": "error", "message": "You can only delete files you own."}), 403
        
    try:
        response = send_tcp_command(PRIMARY_HOST, PRIMARY_PORT, f"DELETE {filename}")
        
        if response == "OK FILE_DELETED":
            try:
                auth.delete_file_and_decrement_quota(filename)
            except Exception as db_err:
                import time
                cleaned = False
                for attempt in range(5):
                    try:
                        time.sleep(0.05 * (attempt + 1))
                        import database
                        conn = database.get_connection()
                        try:
                            with conn:
                                conn.execute("DELETE FROM files WHERE filename = ?", (filename,))
                                user = conn.execute(
                                    "SELECT quota_used_bytes FROM users WHERE id = ?",
                                    (file_record['owner_id'],)
                                ).fetchone()
                                if user:
                                    new_quota = max(0, user["quota_used_bytes"] - file_record['file_size_bytes'])
                                    conn.execute(
                                        "UPDATE users SET quota_used_bytes = ? WHERE id = ?",
                                        (new_quota, file_record['owner_id'])
                                    )
                            cleaned = True
                            break
                        finally:
                            conn.close()
                    except Exception:
                        pass
                if not cleaned:
                    raise db_err
            msg = "File deleted from all servers."
            flash(msg, "success")
            return jsonify({"status": "success", "message": "File deleted from all servers"}), 200
        elif "FILE_NOT_FOUND" in response:
            return jsonify({"status": "error", "message": "File not found"}), 404
        elif "INVALID_FILENAME" in response:
            return jsonify({"status": "error", "message": "Invalid filename"}), 400
        elif "INVALID_COMMAND" in response:
            return jsonify({"status": "error", "message": "Invalid command"}), 400
        elif "REPLICATION_FAILED" in response:
            return jsonify({"status": "error", "message": "Delete failed due to replication propagation error"}), 500
        elif "REPLICATION_AMBIGUOUS" in response:
            return jsonify({"status": "error", "message": "Delete propagation is ambiguous. File removed locally but replica status is unknown."}), 500
        elif "DELETE_FAILED" in response:
            return jsonify({"status": "error", "message": f"Delete failed: {response}"}), 500
        else:
            return jsonify({"status": "error", "message": "Delete failed"}), 500
            
    except Exception as e:
        return jsonify({"status": "error", "message": "Delete failed"}), 500

@app.route('/rename/<filename>', methods=['POST'])
@login_required
def rename_file(filename):
    if g.primary_down:
        return jsonify({
            "status": "error", 
            "message": "Primary server is offline. Write operations are unavailable in failover mode."
        }), 503

    file_record = auth.get_file_by_name(filename)
    if not file_record:
        return jsonify({"status": "error", "message": "File not found"}), 404
        
    if file_record['owner_id'] != g.user['id']:
        return jsonify({"status": "error", "message": "You can only rename files you own."}), 403
        
    new_name = request.form.get('new_name') or request.form.get('new_filename')
    if not new_name and request.is_json:
        new_name = request.json.get('new_name') or request.json.get('new_filename')
        
    if not new_name:
        return jsonify({"status": "error", "message": "New filename is required"}), 400

    # Preserve the user's typed name as the display label before sanitising
    user_display_name = new_name.strip()
    import re
    new_name = os.path.basename(new_name.replace(" ", "_"))
    new_name = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', new_name)  # sanitize special chars
    new_name = re.sub(r'_+', '_', new_name).strip('_-')       # collapse/strip underscores

    # Preserve the original file extension if the user did not include one
    _, original_ext = os.path.splitext(file_record['filename'])
    _, new_ext = os.path.splitext(new_name)
    if original_ext and not new_ext:
        new_name = new_name + original_ext

    if (not new_name or
        new_name in ('.', '..') or
        '..' in new_name or
        new_name.startswith('.') or
        new_name.startswith('-') or
        new_name.endswith('.') or
        new_name.endswith('-') or
        not re.match(r'^[a-zA-Z0-9_\-\.]+$', new_name)):
        return jsonify({"status": "error", "message": "Invalid filename"}), 400
    
    existing_file = auth.get_file_by_name(new_name)
    if existing_file:
        return jsonify({"status": "error", "message": "A file with that name already exists"}), 409
        
    try:
        response = send_tcp_command(PRIMARY_HOST, PRIMARY_PORT, f"RENAME {filename} {new_name}")
        
        if response.startswith("OK FILE_RENAMED"):
            parts = response.split(None, 2)
            actual_new_name = parts[2] if len(parts) > 2 else new_name
            
            try:
                auth.rename_file(filename, actual_new_name, new_original_name=user_display_name)
            except Exception as db_err:
                try:
                    send_tcp_command(PRIMARY_HOST, PRIMARY_PORT, f"RENAME {actual_new_name} {filename}")
                except Exception:
                    pass
                raise db_err
            msg = f"File renamed to {actual_new_name}"
            flash(msg + ".", "success")
            return jsonify({
                "status": "success", 
                "message": msg
            }), 200
        elif "NAME_CONFLICT" in response:
            return jsonify({"status": "error", "message": "A file with that name already exists"}), 409
        elif "FILE_NOT_FOUND" in response:
            return jsonify({"status": "error", "message": "File not found"}), 404
        elif "INVALID_FILENAME" in response:
            return jsonify({"status": "error", "message": "Invalid filename"}), 400
        elif "INVALID_COMMAND" in response:
            return jsonify({"status": "error", "message": "Invalid command"}), 400
        elif "REPLICATION_FAILED" in response:
            return jsonify({"status": "error", "message": "Rename failed due to replication propagation error"}), 500
        elif "RENAME_AMBIGUOUS" in response:
            return jsonify({"status": "error", "message": "Rename propagation is ambiguous. File renamed locally but replica status is unknown."}), 500
        elif "RENAME_FAILED" in response:
            return jsonify({"status": "error", "message": f"Rename failed: {response}"}), 500
        else:
            return jsonify({"status": "error", "message": "Rename failed"}), 500
            
    except Exception as e:
        return jsonify({"status": "error", "message": "Rename failed"}), 500

@app.route('/quota')
@login_required
def get_quota():
    user = auth.get_user_by_id(g.user['id'])
    if not user:
        return jsonify({"status": "error", "message": "User not found"}), 404
    used = user.get('quota_used_bytes', 0) or 0
    limit = user.get('quota_limit_bytes', 0) or 0
    if limit <= 0:
        limit = 52428800  # 50 MB default
    pct = min(100, round(used / limit * 100))
    return jsonify({
        "used": used,
        "limit": limit,
        "used_formatted": format_size(used),
        "limit_formatted": format_size(limit),
        "pct": pct
    })

@app.route('/files')
@login_required
def list_files():
    search = request.args.get('search', '').strip()
    file_type = request.args.get('type', '').strip()
    files = auth.get_filtered_files(search=search, file_type=file_type)
    formatted_files = []
    for f in files:
        formatted_files.append({
            "name": f['filename'],
            "original_name": f['original_name'],
            "size": format_size(f['file_size_bytes']),
            "type": f['file_type'],
            "uploaded": f['uploaded_at'].split('T')[0] if 'T' in f['uploaded_at'] else f['uploaded_at'],
            "owner": f['owner_username'],
            "owner_id": f['owner_id']
        })
    return jsonify({"files": formatted_files})

@app.route('/profile')
@login_required
def profile():
    user = auth.get_user_by_id(g.user['id'])
    if not user:
        flash("User not found.", "error")
        return redirect(url_for('dashboard'))
        
    member_since = "Unknown"
    created_at_str = user.get('created_at')
    if created_at_str:
        try:
            clean_ts = created_at_str.replace('Z', '')
            if 'T' in clean_ts:
                dt = datetime.datetime.fromisoformat(clean_ts)
            else:
                dt = datetime.datetime.strptime(clean_ts, "%Y-%m-%d %H:%M:%S")
            member_since = dt.strftime("%B %d, %Y")
        except Exception:
            member_since = created_at_str
            
    return render_template('profile.html', active_page='profile', user=user, member_since=member_since)

if __name__ == '__main__':
    # Default Flask port is 5000
    debug_mode = os.environ.get("FLASK_DEBUG", "false").lower() in ("true", "1")
    app.run(host='0.0.0.0', port=5000, debug=debug_mode)

