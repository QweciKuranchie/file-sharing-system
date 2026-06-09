from flask import Flask, render_template, request, redirect, url_for, session, flash, g
import os
import datetime
from functools import wraps
import auth

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "super-secret-key-change-in-prod")

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

@app.before_request
def load_user():
    """Runs before every request. Populates g.user from JWT token in session, or clears invalid sessions."""
    g.primary_down = False  # Mock value for FE-1 foundation, will be dynamic in FE-2
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
        
        try:
            token = auth.login_user(username_or_email, password)
            session.permanent = True  # Enforce permanent session lifetime of 30 mins
            session['token'] = token
            return redirect(url_for('dashboard'))
        except ValueError as e:
            flash(str(e), "error")
            
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
        
        if password != confirm_password:
            flash("Passwords do not match.", "error")
            return render_template('register.html', active_page='register')
            
        try:
            auth.register_user(username, email, password)
            flash("Registration successful! Please login.", "success")
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
    return render_template('dashboard.html', active_page='dashboard')

@app.route('/profile')
@login_required
def profile():
    return render_template('profile.html', active_page='profile')

if __name__ == '__main__':
    # Default Flask port is 5000
    app.run(host='0.0.0.0', port=5000, debug=True)
