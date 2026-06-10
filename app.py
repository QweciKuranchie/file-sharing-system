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


app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "super-secret-key-change-in-prod")


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
    line = bytearray()
    while True:
        c = sock.recv(1)
        if not c or not isinstance(c, (bytes, bytearray)):
            break
        line.extend(c)
        if c == b'\n':
            break
    return line.decode('utf-8').strip()

def ping_server(host, port, timeout=5):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        s.sendall(b"PING\n")
        resp = read_line(s)
        return resp == "OK PONG"
    except Exception:
        return False
    finally:
        s.close()

def send_tcp_command(host, port, command_str, file_data=None, timeout=5):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if timeout is not None:
        s.settimeout(timeout)
    try:
        s.connect((host, port))
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
        s.close()

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

@app.before_request
def load_user():
    """Runs before every request. Populates g.user from JWT token in session, or clears invalid sessions."""
    global PRIMARY_DOWN
    
    # Check primary server availability on page/API requests
    if request.endpoint and request.endpoint != 'static':
        PRIMARY_DOWN = not ping_server(PRIMARY_HOST, PRIMARY_PORT, timeout=5)
        
    g.primary_down = PRIMARY_DOWN
    
    token = session.get('token')
    g.user = None
    if token:
        try:
            # Decode and validate the JWT token
            payload = auth.decode_token(token)
            g.user = auth.get_user_by_id(payload['user_id'])
            if not g.user:
                session.pop('token', None)
        except Exception:
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
            flash("Invalid username or password.", "error")
            return render_template('login.html', active_page='login')

        try:
            token = auth.login_user(username_or_email, password)
            session.permanent = True  # Enforce permanent session lifetime of 30 mins
            session['token'] = token
            return redirect(url_for('dashboard'))
        except ValueError as e:
            flash("Invalid username or password.", "error")
            
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
            return render_template('register.html', active_page='register')

        if password != confirm_password:
            flash("Passwords do not match.", "error")
            return render_template('register.html', active_page='register')
            
        try:
            auth.register_user(username, email, password)
            flash("Account created. Please log in.", "success")
            return redirect(url_for('login'))
        except ValueError as e:
            flash(str(e), "error")
            
    return render_template('register.html', active_page='register')

@app.route('/logout')
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    files = auth.get_all_files()
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
    safe_filename = os.path.basename(original_name.replace(" ", "_"))
    if not safe_filename or safe_filename in ('.', '..'):
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
            try:
                send_tcp_command(PRIMARY_HOST, PRIMARY_PORT, f"DELETE {saved_name}")
            except Exception:
                pass
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
    
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(5)
    try:
        s.connect((host, port))
        s.sendall(f"DOWNLOAD {filename}\n".encode('utf-8'))
        
        resp_line = read_line(s)
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
            
            host = REPLICA_HOST
            port = REPLICA_PORT
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(5)
            try:
                s.connect((host, port))
                s.sendall(f"DOWNLOAD {filename}\n".encode('utf-8'))
                
                resp_line = read_line(s)
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
            except Exception as replica_e:
                if s:
                    try:
                        s.close()
                    except Exception:
                        pass
                return jsonify({"status": "error", "message": "Failed to connect to file server."}), 500
        else:
            return jsonify({"status": "error", "message": "Failed to connect to file server."}), 500
        
    def generate_bytes(sock, size):
        try:
            bytes_left = size
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
        
    headers = {
        'Content-Type': mime_type,
        'Content-Disposition': f'attachment; filename="{file_record["original_name"]}"',
        'Content-Length': str(file_size)
    }
    return Response(generate_bytes(s, file_size), headers=headers)

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
        
    new_name = os.path.basename(new_name.replace(" ", "_"))
    if not new_name or new_name in ('.', '..'):
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
                auth.rename_file(filename, actual_new_name)
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
        else:
            return jsonify({"status": "error", "message": "Rename failed"}), 500
            
    except Exception as e:
        return jsonify({"status": "error", "message": "Rename failed"}), 500

@app.route('/files')
@login_required
def list_files():
    files = auth.get_all_files()
    formatted_files = []
    for f in files:
        formatted_files.append({
            "name": f['filename'],
            "size": format_size(f['file_size_bytes']),
            "type": f['file_type'],
            "uploaded": f['uploaded_at'].split('T')[0] if 'T' in f['uploaded_at'] else f['uploaded_at'],
            "owner": f['owner_username']
        })
    return jsonify({"files": formatted_files})

@app.route('/profile')
@login_required
def profile():
    return render_template('profile.html', active_page='profile')

if __name__ == '__main__':
    # Default Flask port is 5000
    app.run(host='0.0.0.0', port=5000, debug=True)

