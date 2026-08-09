import os
import re
import json
import hmac
import secrets
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, flash, redirect, render_template, request, session, url_for, abort, send_from_directory
from flask_sqlalchemy import SQLAlchemy

from authlib.integrations.flask_client import OAuth
from werkzeug.middleware.proxy_fix import ProxyFix
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
 
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
 
app = Flask(__name__, static_folder='static')

app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
 
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)

# Render sets the RENDER env var automatically on every deployed service, so
# this reliably distinguishes "running on Render" from local development
# without needing a separate FLASK_ENV/APP_ENV variable to be configured.
IS_RENDER = bool(os.environ.get("RENDER"))

DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL:
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
    engine_options = {
        "pool_pre_ping": True,
        "pool_recycle": 300,
        "pool_timeout": 30,
    }
    # Supabase's Postgres requires/benefits from SSL. If the DATABASE_URL
    # doesn't already specify an sslmode (e.g. via a `?sslmode=...` query
    # param), request one explicitly rather than leaving it to the driver
    # default. This does not alter the URL itself (host/user/password/port
    # are left untouched) and is skipped entirely if the URL already sets
    # sslmode, so an explicit value in DATABASE_URL always wins.
    connect_args = {}
    if "sslmode" not in DATABASE_URL:
        connect_args["sslmode"] = "require"
    if "connect_timeout" not in DATABASE_URL:
        # Without this, a genuinely unreachable host (e.g. the old IPv6-only
        # Supabase endpoint) can hang for minutes on the OS TCP timeout
        # before failing, stalling app/worker startup instead of failing
        # fast with a clear, loggable error.
        connect_args["connect_timeout"] = 10
    engine_options["connect_args"] = connect_args
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = engine_options
elif IS_RENDER:
    # Never silently fall back to SQLite in production — a missing
    # DATABASE_URL on Render is a misconfiguration, not something to paper
    # over with a non-persistent local database. Fail loudly at startup
    # instead of serving requests against the wrong database.
    raise RuntimeError(
        "DATABASE_URL environment variable is not configured. "
        "Set it in Render's Environment Variables to your Supabase "
        "Session Pooler connection string."
    )
else:
    # Local development only: no DATABASE_URL and not running on Render.
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + os.path.join(BASE_DIR, "app.sqlite3")
    print("WARNING: Using SQLite database - data will not persist on server restart!")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024
# A cookie marked "Secure" is only ever stored/sent by the browser over HTTPS.
# In real deployments (Vercel/Postgres) that's exactly what we want. But the
# app's own local dev server (`python app.py`, see bottom of this file) runs
# on plain HTTP, so a Secure-only cookie would never persist the session —
# every form's CSRF token would silently fail to match on submit, including
# the login form itself, producing a generic "400 Bad Request" page. Only
# relax the flag for that specific local/no-DATABASE_URL case; anything with
# a real DATABASE_URL (i.e. deployed) keeps the Secure cookie unchanged.
app.config["SESSION_COOKIE_SECURE"] = bool(DATABASE_URL)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=24)
 
db = SQLAlchemy(app)
 
oauth = OAuth(app)
google_oauth = oauth.register(
    name="google",
    client_id=os.environ.get("GOOGLE_CLIENT_ID"),
    client_secret=os.environ.get("GOOGLE_CLIENT_SECRET"),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={
        "scope": "openid email profile"
    },
)
# --- END GOOGLE OAUTH ADDITION ---
 
# On Vercel the deployed app bundle is READ-ONLY (only /tmp is writable),
# so uploaded complaint photos can't be saved under static/uploads there.
# NOTE: /tmp on Vercel is EPHEMERAL — files can disappear between requests
# once the serverless instance recycles. This keeps the app from crashing,
# but for real persistent photo storage, plug in Vercel Blob / S3 / Cloudinary
# and store the returned URL instead of a local filename.
if os.environ.get("VERCEL"):
    UPLOAD_DIR = os.path.join("/tmp", "uploads")
else:
    UPLOAD_DIR = os.path.join(BASE_DIR, "static", "uploads")
DB_READY = False
 
ADMIN_EMAIL = "admin@sece.ac.in"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "Admin@1234")
 
STUDENT_CREDENTIALS = {}
 
VALID_CATEGORIES = {"Electrical", "Plumbing", "Wi-Fi", "Cleaning", "Furniture", "Hostel food", "Others"}
VALID_PRIORITIES = {"Low", "Moderate", "High"}
 
try:
    from google import genai
    gemini_api_key = os.environ.get("GEMINI_API_KEY")
    if gemini_api_key:
        genai_client = genai.Client(api_key=gemini_api_key)
    else:
        print("[HOSTEL APP] Warning: GEMINI_API_KEY environment variable not set.")
        genai_client = None
except Exception as e:
    print(f"[HOSTEL APP] Warning: Could not initialize Gemini client: {e}")
    genai_client = None
 
import threading
 
def classify_priority_async(app_instance, complaint_id, description):
    def run():
        with app_instance.app_context():
            try:
                if not genai_client:
                    return
                prompt = (
                    "Determine the priority level of the following hostel complaint description. "
                    "Respond with exactly one word: 'High', 'Moderate', or 'Low'. "
                    "Do not include any other text, explanation, or punctuation.\n\n"
                    f"Complaint Description: {description}"
                )
                response = genai_client.models.generate_content(
                    model="gemini-2.0-flash",
                    contents=prompt
                )
                val = response.text.strip().capitalize()
                if val in {"High", "Moderate", "Low"}:
                    complaint = db.session.get(Complaint, complaint_id)
                    if complaint:
                        complaint.priority = val
                        db.session.commit()
                        print(f"[HOSTEL APP] Updated complaint #{complaint_id} priority to {val} via Gemini async.")
            except Exception as ex:
                print(f"[HOSTEL APP] Async Gemini classification failed for complaint #{complaint_id}: {ex}")
 
    threading.Thread(target=run, daemon=True).start()
VALID_STATUSES = {"Pending", "In Progress", "Resolved"}
VALID_HOSTEL_TYPES = {"boys", "girls"}
VALID_BLOCKS = {"A", "B", "C", "D", "E", "F"}
 
 
def sanitize_string(text, max_length=255):
    if not text:
        return ""
    text = str(text).strip()
    text = re.sub(r'[<>\"\'%;()&+]', '', text)
    return text[:max_length]
 
 
def sanitize_description(text, max_length=500):
    if not text:
        return ""
    text = str(text).strip()
    text = re.sub(r'<[^>]+>', '', text)
    return text[:max_length]
 
 
def determine_priority(description):
    # Local keyword-based priority determination as fallback
    desc_lower = (description or "").lower()
    
    # Critical/urgent issues: safety hazards, major water leaks, complete power/internet failure
    high_keywords = [
        "emergency", "shock", "spark", "short circuit", "fire", "flood", 
        "burst", "no water", "power cut", "power outage", "current", "wire",
        "broken pipe", "blockage", "toilet", "stink", "food poisoning", "poisoning",
        "snake", "urgent", "danger", "hazard", "leakage", "overflow", "injured",
        "accident", "broken glass", "shattered", "lockout", "locked out", "theft", "stolen"
    ]
    
    # Minor, slow, or non-blocking issues
    low_keywords = [
        "slow", "speed", "signal", "dusty", "dirty", "clean", "furniture", 
        "mirror", "paint", "bulb", "fan slow", "wifi slow", "dust"
    ]
    
    fallback_priority = "Moderate"
    for kw in high_keywords:
        if kw in desc_lower:
            fallback_priority = "High"
            break
    if fallback_priority != "High":
        for kw in low_keywords:
            if kw in desc_lower:
                fallback_priority = "Low"
                break
            
    return fallback_priority
 
 
def generate_csrf_token():
    if "_csrf_token" not in session:
        session["_csrf_token"] = secrets.token_hex(32)
    return session["_csrf_token"]


app.jinja_env.globals["csrf_token"] = generate_csrf_token

CSRF_EXEMPT_ENDPOINTS = {"google_authorize"}


@app.before_request
def _csrf_protect():
    if request.method == "POST" and request.endpoint not in CSRF_EXEMPT_ENDPOINTS:
        token = session.get("_csrf_token")
        form_token = request.form.get("csrf_token")
        if not token or not form_token or not hmac.compare_digest(token, form_token):
            abort(400)


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user" not in session:
            flash("Please login first.", "error")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated_function
 
 
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get("role") == "admin":
            abort(403)
        return f(*args, **kwargs)
    return decorated_function
 
 
class Complaint(db.Model):
    __tablename__ = "complaints"
    id = db.Column(db.Integer, primary_key=True)
    student_email = db.Column(db.String(120), nullable=False, index=True)
    student_name = db.Column(db.String(120), nullable=False)
    room_number = db.Column(db.String(50), nullable=False)
    floor = db.Column(db.String(10), nullable=True)
    category = db.Column(db.String(50), nullable=False)
    priority = db.Column(db.String(10), nullable=False)
    description = db.Column(db.Text, nullable=False)
    hostel_type = db.Column(db.String(10), nullable=True)
    block = db.Column(db.String(5), nullable=True)
    image_filename = db.Column(db.String(255), nullable=True)
    status = db.Column(db.String(20), nullable=False, default="Pending")
    admin_note = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
 
    def to_dict(self):
        return {
            "id": self.id,
            "student_email": self.student_email,
            "student_name": self.student_name,
            "room_number": self.room_number,
            "floor": self.floor,
            "category": self.category,
            "priority": self.priority,
            "description": self.description,
            "hostel_type": self.hostel_type,
            "block": self.block,
            "image_filename": self.image_filename,
            "status": self.status,
            "admin_note": self.admin_note,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
 
 
class IssueCounter(db.Model):
    __tablename__ = "issue_counter"
    id = db.Column(db.Integer, primary_key=True)
    total = db.Column(db.Integer, nullable=False, default=0)
 
    @classmethod
    def get(cls):
        row = cls.query.first()
        if not row:
            row = cls(total=0)
            db.session.add(row)
            db.session.commit()
        return row
 
 
class StudentProfile(db.Model):
    __tablename__ = "student_profiles"
    email = db.Column(db.String(120), primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    roll_number = db.Column(db.String(50), nullable=True)
    dept = db.Column(db.String(100), nullable=True)
    year = db.Column(db.String(10), nullable=True)
    phone = db.Column(db.String(20), nullable=True)
    password = db.Column(db.String(255), nullable=True)
    hostel_type = db.Column(db.String(10), nullable=True)
    block = db.Column(db.String(10), nullable=True)
    floor = db.Column(db.String(10), nullable=True)
    room_no = db.Column(db.String(20), nullable=True)
 
 
class Warden(db.Model):
    __tablename__ = "wardens"
    id = db.Column(db.Integer, primary_key=True)
    hostel_type = db.Column(db.String(10), nullable=False) # 'boys' or 'girls'
    block = db.Column(db.String(5), nullable=False)        # 'A', 'B', 'C', 'D', 'E', 'F'
    name = db.Column(db.String(120), nullable=False)
    contact = db.Column(db.String(50), nullable=False)
 
    def to_dict(self):
        return {
            "id": self.id,
            "hostel_type": self.hostel_type,
            "block": self.block,
            "name": self.name,
            "contact": self.contact
        }
 
 
class Notice(db.Model):
    __tablename__ = "notices"
    id = db.Column(db.Integer, primary_key=True)
    hostel_type = db.Column(db.String(10), nullable=False) # 'boys', 'girls', or 'all'
    block = db.Column(db.String(10), nullable=False)       # 'A', 'B', 'C', 'D', 'E', 'F', or 'all'
    title = db.Column(db.String(120), nullable=False)
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    resolved_by = db.Column(db.DateTime, nullable=True)
 
    def to_dict(self):
        return {
            "id": self.id,
            "hostel_type": self.hostel_type,
            "block": self.block,
            "title": self.title,
            "content": self.content,
            "created_at": self.created_at.strftime('%Y-%m-%d %H:%M:%S') if self.created_at else "",
            "resolved_by": self.resolved_by.strftime('%Y-%m-%d %H:%M:%S') if self.resolved_by else ""
        }
 
 
def _allowed_image(filename):
    if not filename or "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[1].lower()
    return ext in {"png", "jpg", "jpeg", "webp", "gif"}
 
 
def _init_db():
    global DB_READY
    if not DB_READY:
        db_type = "PostgreSQL" if os.environ.get("DATABASE_URL") else "SQLite"
        print(f"[HOSTEL APP] Database configuration loaded ({db_type}).")
        print("[HOSTEL APP] Database initialization started...")
        try:
            db.create_all()
            print("[HOSTEL APP] Database connection successful.")
        except Exception as e:
            # With multiple Gunicorn workers, each worker runs this startup
            # code independently and they can race to create the schema at
            # the same moment. If another worker already won that race, this
            # specific error is benign — the schema exists, which is exactly
            # what we wanted — so treat it as success rather than a real
            # connectivity failure. Anything else (e.g. an unreachable host)
            # is re-raised so it's reported and handled the normal way.
            if "already exists" in str(e).lower():
                db.session.rollback()
                print("[HOSTEL APP] Database connection successful (schema already initialized by another worker).")
            else:
                raise
 
        columns_to_add = [
            ("password", "VARCHAR(255)"),
            ("hostel_type", "VARCHAR(10)"),
            ("block", "VARCHAR(10)"),
            ("floor", "VARCHAR(10)"),
            ("room_no", "VARCHAR(20)")
        ]
        for col_name, col_type in columns_to_add:
            try:
                db.session.execute(db.text(f"SELECT {col_name} FROM student_profiles LIMIT 1"))
            except Exception:
                db.session.rollback()
                print(f"[HOSTEL APP] Adding column '{col_name}' to 'student_profiles'...")
                try:
                    db.session.execute(db.text(f"ALTER TABLE student_profiles ADD COLUMN {col_name} {col_type}"))
                    db.session.commit()
                except Exception as e:
                    print(f"[HOSTEL APP] Error adding column '{col_name}': {e}")
                    db.session.rollback()
 
        # Add floor to complaints table if not present
        try:
            db.session.execute(db.text("SELECT floor FROM complaints LIMIT 1"))
        except Exception:
            db.session.rollback()
            print("[HOSTEL APP] Adding column 'floor' to 'complaints'...")
            try:
                db.session.execute(db.text("ALTER TABLE complaints ADD COLUMN floor VARCHAR(10)"))
                db.session.commit()
            except Exception as e:
                print(f"[HOSTEL APP] Error adding column 'floor' to 'complaints': {e}")
                db.session.rollback()
 
        # Add resolved_by to notices table if not present
        try:
            db.session.execute(db.text("SELECT resolved_by FROM notices LIMIT 1"))
        except Exception:
            db.session.rollback()
            print("[HOSTEL APP] Adding column 'resolved_by' to 'notices'...")
            try:
                db.session.execute(db.text("ALTER TABLE notices ADD COLUMN resolved_by TIMESTAMP"))
                db.session.commit()
            except Exception as e:
                print(f"[HOSTEL APP] Error adding column 'resolved_by' to 'notices': {e}")
                db.session.rollback()
 
        # Initialize default wardens if table is empty
        try:
            db.session.execute(db.text("SELECT 1 FROM wardens LIMIT 1"))
        except Exception:
            db.session.rollback()
            print("[HOSTEL APP] Table 'wardens' might not exist or need creation...")
            
        try:
            if Warden.query.count() == 0:
                default_wardens = [
                    # Boys Hostel
                    Warden(hostel_type="boys", block="A", name="Mr. Rajesh Kumar", contact="+91 98765 43210"),
                    Warden(hostel_type="boys", block="B", name="Mr. Suresh Raina", contact="+91 98765 43211"),
                    Warden(hostel_type="boys", block="C", name="Mr. Amit Sharma", contact="+91 98765 43212"),
                    Warden(hostel_type="boys", block="D", name="Mr. Vijay Singh", contact="+91 98765 43213"),
                    Warden(hostel_type="boys", block="E", name="Mr. Dinesh Karthik", contact="+91 98765 43214"),
                    Warden(hostel_type="boys", block="F", name="Mr. Ramesh Sen", contact="+91 98765 43215"),
                    # Girls Hostel
                    Warden(hostel_type="girls", block="A", name="Mrs. Priya Patel", contact="+91 98765 43216"),
                    Warden(hostel_type="girls", block="B", name="Mrs. Lakshmi Roy", contact="+91 98765 43217"),
                    Warden(hostel_type="girls", block="C", name="Mrs. Sunita Rao", contact="+91 98765 43218"),
                    Warden(hostel_type="girls", block="D", name="Mrs. Anita Desai", contact="+91 98765 43219"),
                    Warden(hostel_type="girls", block="E", name="Mrs. Radha Krishnan", contact="+91 98765 43220"),
                    Warden(hostel_type="girls", block="F", name="Mrs. Deepa Nair", contact="+91 98765 43221"),
                ]
                db.session.bulk_save_objects(default_wardens)
                db.session.commit()
                print("[HOSTEL APP] Default wardens initialized.")
        except Exception as e:
            print(f"[HOSTEL APP] Error initializing wardens: {e}")
            db.session.rollback()
 
        # Initialize default notices if table is empty
        try:
            if Notice.query.count() == 0:
                default_notices = [
                    Notice(hostel_type="all", block="B", title="Water Interruption", content="Block B — 6 AM–9 AM every Sunday."),
                    Notice(hostel_type="all", block="all", title="Wi-Fi Upgrade", content="New routers on floors 3 & 4."),
                    Notice(hostel_type="all", block="all", title="Response SLA", content="All issues resolved within 24 hrs."),
                ]
                db.session.bulk_save_objects(default_notices)
                db.session.commit()
                print("[HOSTEL APP] Default notices initialized.")
        except Exception as e:
            print(f"[HOSTEL APP] Error initializing notices: {e}")
            db.session.rollback()
 
        counter = IssueCounter.get()
        if counter.total == 0:
            existing = Complaint.query.count()
            if existing:
                counter.total = existing
                db.session.commit()
        print(f"[HOSTEL APP] Database ready. Total complaints: {IssueCounter.get().total}")
        DB_READY = True
 
 
@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


# --- Startup-time database initialization ---
# This runs exactly once, when the application module is loaded (i.e. when
# Gunicorn boots a worker, or when `python app.py` starts locally) — NOT on
# every incoming request. This is deliberate: initializing (and, before this
# fix, potentially failing to connect) on every request meant an unrelated
# request like `/static/SECElogo.png` could 500 because of a transient
# database problem. Static files are served directly by Flask/Werkzeug and
# never touch this code path.
#
# If the database is unreachable at startup, the error is logged clearly and
# the application still comes up so static assets and the /health endpoint
# keep working; database-backed routes will surface their own errors (caught
# by the existing 500 handler below) until connectivity is restored.
DB_INIT_ERROR = None
try:
    os.makedirs(UPLOAD_DIR, exist_ok=True)
except OSError:
    pass

with app.app_context():
    try:
        _init_db()
        print("[HOSTEL APP] Application startup complete.")
    except Exception as e:
        DB_INIT_ERROR = str(e)
        db.session.rollback()
        print("[HOSTEL APP] ERROR: Database connection failed at startup.")
        print("[HOSTEL APP] Check DATABASE_URL and Supabase connectivity.")
        print(f"[HOSTEL APP] Details: {e}")


@app.route("/health")
def health():
    """Liveness/readiness check for Render and manual debugging. Always
    performs a fresh query rather than trusting a cached flag, so it
    reflects the database's *current* reachability (e.g. after a transient
    Supabase outage recovers)."""
    try:
        db.session.execute(db.text("SELECT 1"))
        return {"status": "healthy", "database": "connected"}, 200
    except Exception as e:
        db.session.rollback()
        return {"status": "unhealthy", "database": "unavailable", "detail": str(e)}, 503
 
 
@app.after_request
def add_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response
 
 
@app.route("/")
def login():
    if "user" in session:
        return redirect(url_for("welcome"))
    return render_template("login.html")
 
 
@app.route("/login", methods=["POST"])
def handle_login():
    email = (request.form.get("email") or "").strip().lower()
    password = (request.form.get("password") or "").strip()
 
    if not email or not password:
        return render_template("login.html", error="Please enter email and password.")
 
    email = sanitize_string(email, 120)
    
    if not re.match(r'^[\w\.-]+@[\w\.-]+\.\w+$', email):
        return render_template("login.html", error="Invalid email format.")
 
    if email == ADMIN_EMAIL and password == ADMIN_PASSWORD:
        session.permanent = True
        session["user"] = email
        session["role"] = "admin"
        session["full_name"] = "Administrator"
        session["login_time"] = datetime.utcnow().isoformat()
        return redirect(url_for("welcome"))
 
    if email in STUDENT_CREDENTIALS:
        display_name, correct_pw = STUDENT_CREDENTIALS[email]
        if password == correct_pw:
            session.permanent = True
            session["user"] = email
            session["role"] = "student"
            session["full_name"] = display_name
            session["login_time"] = datetime.utcnow().isoformat()
            return redirect(url_for("welcome"))
        return render_template("login.html", error="Incorrect password. Please try again.")
 
    # Check DB profile next
    profile = StudentProfile.query.filter_by(email=email).first()
    if profile:
        if profile.password and password == profile.password:
            session.permanent = True
            session["user"] = email
            session["role"] = "student"
            session["full_name"] = profile.name
            session["login_time"] = datetime.utcnow().isoformat()
            return redirect(url_for("welcome"))
        return render_template("login.html", error="Incorrect password. Please try again.")
 
    return render_template("login.html", error="Email not registered. Use your official SECE email.")
 

@app.route("/login/google")
def google_login():
    if "user" in session:
        return redirect(url_for("welcome"))
    if not (os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET")):
        flash("Google sign-in is not configured on this server. Please use your email and password instead.", "error")
        return redirect(url_for("login"))
    redirect_uri = url_for("google_authorize", _external=True)
    try:
        return google_oauth.authorize_redirect(redirect_uri)
    except Exception:
        flash("Google sign-in is temporarily unavailable. Please use your email and password instead.", "error")
        return redirect(url_for("login"))


@app.route("/login/google/callback")
def google_authorize():
    try:
        token = google_oauth.authorize_access_token()
    except Exception:
        flash("Google sign-in failed or was cancelled. Please try again.", "error")
        return redirect(url_for("login"))
 
    userinfo = token.get("userinfo") or {}
    if not userinfo:
        try:
            userinfo = google_oauth.parse_id_token(token, nonce=session.get("oauth_nonce")) or {}
        except Exception:
            userinfo = {}
 
    email = (userinfo.get("email") or "").strip().lower()
    full_name = sanitize_string(userinfo.get("name") or "", 120)
    picture = userinfo.get("picture") or ""
    if not email or not email.endswith("@sece.ac.in"):
        session.clear()
        flash("Only SECE email accounts are allowed to access this website.", "error")
        return redirect(url_for("login"))
 
    # Valid SECE account: establish a fresh, secure session.
    session.clear()
    session.permanent = True
    session["user"] = email
    session["full_name"] = full_name or email.split("@")[0]
    session["picture"] = picture
    session["login_time"] = datetime.utcnow().isoformat()
    session["role"] = "admin" if email == ADMIN_EMAIL else "student"
    session["auth_provider"] = "google"
    return redirect(url_for("welcome"))
 
 
@app.route("/signup", methods=["POST"])
def handle_signup():
    name = sanitize_string(request.form.get("name", ""), 120).strip()
    roll_number = sanitize_string(request.form.get("roll_number", ""), 50).strip()
    dept = sanitize_string(request.form.get("dept", ""), 100).strip()
    year = sanitize_string(request.form.get("year", ""), 10).strip()
    phone = sanitize_string(request.form.get("phone", ""), 20).strip()
    email = sanitize_string(request.form.get("email", ""), 120).lower().strip()
    password = (request.form.get("password") or "").strip()
    hostel_type = sanitize_string(request.form.get("hostel_type", ""), 10).strip()
    block = sanitize_string(request.form.get("block", ""), 10).strip()
    floor = sanitize_string(request.form.get("floor", ""), 10).strip()
    room_no = sanitize_string(request.form.get("room_no", ""), 20).strip()
 
    if not (name and roll_number and dept and year and phone and email and password and hostel_type and block and floor and room_no):
        return render_template("login.html", error="All fields are required.", show_signup=True)
 
    if not email.endswith("@sece.ac.in"):
        return render_template("login.html", error="Use your college official email", show_signup=True)
 
    # Validate phone number (must be 10 digits)
    if not re.match(r'^\d{10}$', phone):
        return render_template("login.html", error="Phone number must be a 10-digit number.", show_signup=True)
 
    # Check if user already exists
    if email in STUDENT_CREDENTIALS:
        return render_template("login.html", error="Email is already registered. Please log in.", show_signup=False)
 
    existing = StudentProfile.query.filter_by(email=email).first()
    if existing:
        return render_template("login.html", error="Email is already registered. Please log in.", show_signup=False)
 
    try:
        new_profile = StudentProfile(
            email=email,
            name=name,
            roll_number=roll_number,
            dept=dept,
            year=year,
            phone=phone,
            password=password,
            hostel_type=hostel_type,
            block=block,
            floor=floor,
            room_no=room_no
        )
        db.session.add(new_profile)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return render_template("login.html", error=f"Registration failed: {str(e)}", show_signup=True)
 
    return render_template("login.html", success="Registration successful! Please log in with your credentials.", show_signup=False)
 
 
VALID_HOSTEL_TYPES = {"boys", "girls"}
VALID_BLOCK_LETTERS = {"A", "B", "C", "D", "E", "F"}


@app.route("/welcome")
@login_required
def welcome():
    return _render_dashboard()


@app.route("/admin/hostel/<hostel_type>")
@login_required
@admin_required
def admin_hostel_view(hostel_type):
    hostel_type = (hostel_type or "").strip().lower()
    if hostel_type not in VALID_HOSTEL_TYPES:
        abort(404)
    return _render_dashboard(scope_hostel=hostel_type, scope_block="all")


@app.route("/admin/hostel/<hostel_type>/block-<block_letter>")
@login_required
@admin_required
def admin_hostel_block_view(hostel_type, block_letter):
    hostel_type = (hostel_type or "").strip().lower()
    block_letter = (block_letter or "").strip().upper()
    if hostel_type not in VALID_HOSTEL_TYPES or block_letter not in VALID_BLOCK_LETTERS:
        abort(404)
    return _render_dashboard(scope_hostel=hostel_type, scope_block=block_letter)


# ---------------------------------------------------------------------------
# Hostel block directory + individual block detail pages.
# One shared query/render helper is used for both hostel types so there is
# no duplicated backend logic; the template layer still gets a dedicated
# file per block (boys_block_a.html ... girls_block_f.html) as required.
# ---------------------------------------------------------------------------

def _block_summary(hostel_type, block_letter, role=None, viewer_email=None):
    """Real, DB-backed summary for a single block. Never fabricates data —
    any metric the schema doesn't track is simply omitted (None)."""
    students = StudentProfile.query.filter_by(hostel_type=hostel_type, block=block_letter).all()
    total_students = len(students)

    floor_counts = {}
    room_counts = {}
    for s in students:
        f = (s.floor or "Unspecified").strip() or "Unspecified"
        floor_counts[f] = floor_counts.get(f, 0) + 1
        r = (s.room_no or "").strip()
        if r:
            room_counts[r] = room_counts.get(r, 0) + 1

    warden = Warden.query.filter_by(hostel_type=hostel_type, block=block_letter).first()

    complaints_q = Complaint.query.filter_by(hostel_type=hostel_type, block=block_letter)
    total_complaints = complaints_q.count()
    pending = complaints_q.filter_by(status="Pending").count()
    in_progress = complaints_q.filter_by(status="In Progress").count()
    resolved = complaints_q.filter_by(status="Resolved").count()

    # ---- Analytics scoped to THIS block only ---------------------------
    from sqlalchemy import func

    block_complaints_all = Complaint.query.filter_by(
        hostel_type=hostel_type, block=block_letter
    ).order_by(Complaint.created_at.desc()).all()

    cat_rows = (
        db.session.query(Complaint.category, func.count(Complaint.id))
        .filter(Complaint.hostel_type == hostel_type, Complaint.block == block_letter)
        .group_by(Complaint.category)
        .all()
    )
    cat_labels = [r[0] for r in cat_rows]
    cat_counts = [r[1] for r in cat_rows]

    pri_rows = (
        db.session.query(Complaint.priority, func.count(Complaint.id))
        .filter(Complaint.hostel_type == hostel_type, Complaint.block == block_letter)
        .group_by(Complaint.priority)
        .all()
    )
    pri_dict = {r[0]: r[1] for r in pri_rows}

    high_priority_open = sum(
        1 for c in block_complaints_all if c.priority == "High" and c.status != "Resolved"
    )

    now = datetime.utcnow()
    month_labels, monthly_issued, monthly_resolved = [], [], []
    for i in range(5, -1, -1):
        m = now.month - i
        y = now.year
        while m <= 0:
            m += 12
            y -= 1
        month_labels.append(datetime(y, m, 1).strftime("%b %Y"))
        issued = sum(1 for c in block_complaints_all if c.created_at.month == m and c.created_at.year == y)
        resolved_m = sum(
            1 for c in block_complaints_all
            if c.status == "Resolved" and c.updated_at.month == m and c.updated_at.year == y
        )
        monthly_issued.append(issued)
        monthly_resolved.append(resolved_m)

    # ---- Complaint management table (admins only see everyone's;
    #      students only ever see their own — never fabricated, never leaked). ----
    if role == "admin":
        complaints_for_table = block_complaints_all
    elif viewer_email:
        complaints_for_table = [c for c in block_complaints_all if c.student_email == viewer_email]
    else:
        complaints_for_table = []

    return {
        "hostel_type": hostel_type,
        "block": block_letter,
        "total_students": total_students,
        "floor_counts": sorted(floor_counts.items()),
        "room_counts": sorted(room_counts.items()),
        "warden": warden,
        "complaint_stats": {
            "total": total_complaints,
            "pending": pending,
            "in_progress": in_progress,
            "resolved": resolved,
        },
        "block_complaints": complaints_for_table,
        "block_complaints_json": json.dumps([c.to_dict() for c in complaints_for_table]),
        "high_priority_open": high_priority_open,
        "cat_labels": json.dumps(cat_labels),
        "cat_counts": json.dumps(cat_counts),
        "pri_dict": json.dumps(pri_dict),
        "month_labels": json.dumps(month_labels),
        "monthly_issued": json.dumps(monthly_issued),
        "monthly_resolved": json.dumps(monthly_resolved),
    }


@app.route("/boys")
@app.route("/girls")
@login_required
def hostel_blocks_list():
    hostel_type = "boys" if request.path == "/boys" else "girls"
    blocks = []
    for letter in sorted(VALID_BLOCK_LETTERS):
        summary = _block_summary(hostel_type, letter)
        blocks.append(summary)
    return render_template(
        "hostel_blocks_list.html",
        email=session["user"],
        full_name=session.get("full_name", ""),
        role=session.get("role"),
        hostel_type=hostel_type,
        blocks=blocks,
    )


@app.route("/boys/block-<letter>")
@app.route("/girls/block-<letter>")
@login_required
def hostel_block_detail(letter):
    hostel_type = "boys" if request.path.startswith("/boys/") else "girls"
    block_letter = (letter or "").strip().upper()
    if block_letter not in VALID_BLOCK_LETTERS:
        abort(404)

    summary = _block_summary(
        hostel_type, block_letter,
        role=session.get("role"), viewer_email=session.get("user"),
    )
    template_name = f"{hostel_type}_block_{block_letter.lower()}.html"

    return render_template(
        template_name,
        email=session["user"],
        full_name=session.get("full_name", ""),
        role=session.get("role"),
        **summary,
    )


def _render_dashboard(scope_hostel="all", scope_block="all"):
    cumulative_total = IssueCounter.get().total
    wardens = Warden.query.all()
    notices = []
 
    if session.get("role") == "admin":
        complaints_query = Complaint.query.order_by(Complaint.created_at.desc())
        if scope_hostel != "all":
            complaints_query = complaints_query.filter(Complaint.hostel_type == scope_hostel)
        if scope_block != "all":
            complaints_query = complaints_query.filter(Complaint.block == scope_block)
        complaints = complaints_query.all()
 
        from sqlalchemy import func
        cat_rows = db.session.query(Complaint.category, func.count(Complaint.id)).group_by(Complaint.category).all()
        cat_labels = [r[0] for r in cat_rows]
        cat_counts = [r[1] for r in cat_rows]
 
        pri_rows = db.session.query(Complaint.priority, func.count(Complaint.id)).group_by(Complaint.priority).all()
        pri_dict = {r[0]: r[1] for r in pri_rows}
 
        # Block-wise complaint data for admin
        block_rows = db.session.query(Complaint.block, func.count(Complaint.id)).filter(Complaint.block != None).group_by(Complaint.block).all()
        block_data = {r[0]: r[1] for r in block_rows}
        # Also get block+status breakdown
        block_status_rows = db.session.query(Complaint.block, Complaint.status, func.count(Complaint.id)).filter(Complaint.block != None).group_by(Complaint.block, Complaint.status).all()
        block_status_data = {}
        for blk, sts, cnt in block_status_rows:
            if blk not in block_status_data:
                block_status_data[blk] = {}
            block_status_data[blk][sts] = cnt
 
        # Hostel-type (Boys / Girls) split for bar chart
        hostel_rows = db.session.query(
            Complaint.hostel_type, Complaint.category, func.count(Complaint.id)
        ).filter(Complaint.hostel_type != None).group_by(Complaint.hostel_type, Complaint.category).all()
        hostel_category_data = {"boys": {}, "girls": {}}
        for ht, cat, cnt in hostel_rows:
            if ht in hostel_category_data:
                hostel_category_data[ht][cat] = cnt
 
        # Simple totals per hostel for the summary bar chart
        hostel_total_rows = db.session.query(Complaint.hostel_type, func.count(Complaint.id)).filter(Complaint.hostel_type != None).group_by(Complaint.hostel_type).all()
        hostel_totals = {r[0]: r[1] for r in hostel_total_rows}
 
        # Hostel status breakdown
        hostel_status_rows = db.session.query(Complaint.hostel_type, Complaint.status, func.count(Complaint.id)).filter(Complaint.hostel_type != None).group_by(Complaint.hostel_type, Complaint.status).all()
        hostel_status_data = {"boys": {}, "girls": {}}
        for ht, sts, cnt in hostel_status_rows:
            if ht in hostel_status_data:
                hostel_status_data[ht][sts] = cnt
 
        # Hostel + Block cross data: {hostel_type: {block: {status: count}}}
        hostel_block_rows = db.session.query(
            Complaint.hostel_type, Complaint.block, Complaint.status, func.count(Complaint.id)
        ).filter(Complaint.hostel_type != None, Complaint.block != None).group_by(
            Complaint.hostel_type, Complaint.block, Complaint.status
        ).all()
        hostel_block_data = {"boys": {}, "girls": {}}
        for ht, blk, sts, cnt in hostel_block_rows:
            if ht in hostel_block_data:
                if blk not in hostel_block_data[ht]:
                    hostel_block_data[ht][blk] = {}
                hostel_block_data[ht][blk][sts] = cnt
 
        now = datetime.utcnow()
        month_labels, monthly_issued, monthly_resolved = [], [], []
        for i in range(5, -1, -1):
            m = now.month - i
            y = now.year
            while m <= 0:
                m += 12
                y -= 1
            month_labels.append(datetime(y, m, 1).strftime("%b %Y"))
            issued = sum(1 for c in complaints if c.created_at.month == m and c.created_at.year == y)
            resolved = sum(1 for c in complaints if c.status == "Resolved" and c.updated_at.month == m and c.updated_at.year == y)
            monthly_issued.append(issued)
            monthly_resolved.append(resolved)
    else:
        complaints = Complaint.query.filter_by(student_email=session["user"]).order_by(Complaint.created_at.desc()).all()
        cat_labels = cat_counts = month_labels = monthly_issued = monthly_resolved = []
        pri_dict = {}
        block_data = {}
        block_status_data = {}
        hostel_category_data = {}
        hostel_totals = {}
        hostel_status_data = {}
        hostel_block_data = {}
 
    active = len(complaints)
    pending = sum(1 for c in complaints if c.status == "Pending")
    in_progress = sum(1 for c in complaints if c.status == "In Progress")
    resolved = sum(1 for c in complaints if c.status == "Resolved")
 
    # For admin: total ever (global counter). For student: their own total.
    if session.get("role") == "admin":
        display_total = IssueCounter.get().total
    else:
        display_total = active
 
    stats = dict(total=display_total, active=active, pending=pending, in_progress=in_progress, resolved=resolved)
 
    profile = None
    if session.get("role") == "student":
        profile = StudentProfile.query.filter_by(email=session["user"]).first()
        if not profile:
            display_name = session.get("full_name", "")
            profile = StudentProfile(
                email=session["user"],
                name=display_name,
                roll_number="",
                dept="",
                year="",
                phone=""
            )
            db.session.add(profile)
            db.session.commit()
 
        hostel = (profile.hostel_type or 'all').lower()
        blk = (profile.block or 'all').upper()
        notices = Notice.query.filter(
            Notice.hostel_type.in_(['all', 'ALL', hostel])
        ).filter(
            Notice.block.in_(['all', 'ALL', blk])
        ).order_by(Notice.created_at.desc()).all()
 
    return render_template(
        "welcome.html", email=session["user"], full_name=session.get("full_name", ""),
        role=session.get("role"), complaints=complaints, stats=stats,
        cat_labels=json.dumps(cat_labels), cat_counts=json.dumps(cat_counts),
        pri_dict=json.dumps(pri_dict), month_labels=json.dumps(month_labels),
        monthly_issued=json.dumps(monthly_issued), monthly_resolved=json.dumps(monthly_resolved),
        block_data=json.dumps(block_data), block_status_data=json.dumps(block_status_data),
        hostel_category_data=json.dumps(hostel_category_data),
        hostel_totals=json.dumps(hostel_totals),
        hostel_status_data=json.dumps(hostel_status_data),
        hostel_block_data=json.dumps(hostel_block_data),
        profile=profile,
        complaints_json=json.dumps([c.to_dict() for c in complaints]),
        wardens=wardens,
        notices=notices,
        scope_hostel=scope_hostel,
        scope_block=scope_block
    )
 
 
@app.route("/complaint")
@login_required
def complaint():
    if session.get("role") == "admin":
        flash("Admins cannot submit complaints.")
        return redirect(url_for("welcome"))
    profile = StudentProfile.query.filter_by(email=session["user"]).first()
    return render_template("complaint.html", full_name=session.get("full_name", ""), profile=profile)
 
 
@app.route("/submit_complaint", methods=["POST"])
@login_required
def submit_complaint():
    if session.get("role") == "admin":
        return redirect(url_for("welcome"))
 
    student_name = sanitize_string(request.form.get("name", ""), 120)
    room_number = sanitize_string(request.form.get("room", ""), 50)
    floor = sanitize_string(request.form.get("floor", ""), 10).strip()
    hostel_type_raw = (request.form.get("hostel_type") or "").strip().lower()
    block_raw = (request.form.get("block") or "").strip().upper()
    category_raw = (request.form.get("category") or "").strip()
    other_category_raw = (request.form.get("other_category") or "").strip()
    description = sanitize_description(request.form.get("description", ""), 500)
 
    hostel_type = hostel_type_raw if hostel_type_raw in VALID_HOSTEL_TYPES else None
    block = block_raw if block_raw in VALID_BLOCKS else None
    
    if category_raw == "Others":
        if other_category_raw:
            category = f"Others: {other_category_raw}"[:50]
        else:
            category = "Others"
    else:
        category = category_raw if category_raw in VALID_CATEGORIES else None
        
    priority = determine_priority(description)
 
    errors = []
    if not student_name:
        errors.append("Student name is required.")
    if not room_number:
        errors.append("Room number is required.")
    if not floor:
        errors.append("Floor details are required.")
    if not category:
        errors.append("Please select a valid category.")
    if not description:
        errors.append("Description is required.")
 
    if errors:
        for error in errors:
            flash(error, "error")
        return redirect(url_for("complaint"))
 
    c = Complaint(
        student_email=session["user"], student_name=student_name, room_number=room_number, floor=floor,
        hostel_type=hostel_type, block=block, category=category, priority=priority,
        description=description, status="Pending",
    )
    db.session.add(c)
    db.session.flush()
 
    counter = IssueCounter.get()
    counter.total += 1
    db.session.commit()
 
    uploaded = request.files.get("image")
    if uploaded and uploaded.filename and uploaded.filename.strip():
        if _allowed_image(uploaded.filename):
            ext = uploaded.filename.rsplit(".", 1)[1].lower()
            final_filename = f"{c.id}_{secrets.token_hex(8)}.{ext}"
            uploaded.save(os.path.join(UPLOAD_DIR, final_filename))
            c.image_filename = final_filename
            db.session.commit()
        else:
            flash("Invalid image type. PNG, JPG, JPEG, WEBP allowed.", "warning")
 
    if genai_client:
        classify_priority_async(app, c.id, description)
 
    flash("Complaint submitted successfully!", "success")
    return redirect(url_for("welcome", _anchor="complaints"))
 
 
@app.route("/update_profile", methods=["POST"])
@login_required
def update_profile():
    if session.get("role") != "student":
        abort(403)
        
    email = sanitize_string(request.form.get("email", ""), 120).lower().strip()
    name = sanitize_string(request.form.get("name", ""), 120).strip()
    roll_number = sanitize_string(request.form.get("roll_number", ""), 50).strip()
    dept = sanitize_string(request.form.get("dept", ""), 100).strip()
    year = sanitize_string(request.form.get("year", ""), 10).strip()
    phone = sanitize_string(request.form.get("phone", ""), 20).strip()
    hostel_type = sanitize_string(request.form.get("hostel_type", ""), 10).strip()
    block = sanitize_string(request.form.get("block", ""), 10).strip()
    floor = sanitize_string(request.form.get("floor", ""), 10).strip()
    room_no = sanitize_string(request.form.get("room_no", ""), 20).strip()
    
    if not (name and hostel_type and block and floor and room_no):
        flash("Name, hostel type, block, floor, and room number are required.", "error")
        return redirect(url_for("welcome"))
        
    if not email.endswith("@sece.ac.in"):
        flash("Official email must be a @sece.ac.in domain.", "error")
        return redirect(url_for("welcome"))
        
    profile = StudentProfile.query.filter_by(email=session["user"]).first()
    if not profile:
        profile = StudentProfile(email=session["user"])
        db.session.add(profile)
        
    if profile.email != email:
        existing = StudentProfile.query.filter_by(email=email).first()
        if existing:
            flash("Email is already registered to another profile.", "error")
            return redirect(url_for("welcome"))
            
        old_password = profile.password
        Complaint.query.filter_by(student_email=profile.email).update({Complaint.student_email: email})
        db.session.delete(profile)
        db.session.flush()
        
        profile = StudentProfile(
            email=email,
            name=name,
            roll_number=roll_number,
            dept=dept,
            year=year,
            phone=phone,
            password=old_password,
            hostel_type=hostel_type,
            block=block,
            floor=floor,
            room_no=room_no
        )
        db.session.add(profile)
        session["user"] = email
    else:
        profile.name = name
        profile.roll_number = roll_number
        profile.dept = dept
        profile.year = year
        profile.phone = phone
        profile.hostel_type = hostel_type
        profile.block = block
        profile.floor = floor
        profile.room_no = room_no
        
    session["full_name"] = name
    db.session.commit()
    flash("Profile updated successfully!", "success")
    return redirect(url_for("welcome"))
 
 
@app.route("/admin/student_details/<email>")
@login_required
@admin_required
def admin_student_details(email):
    profile = StudentProfile.query.filter_by(email=email).first()
    if not profile:
        return {"error": "Student profile not found"}, 404
    return {
        "name": profile.name,
        "email": profile.email,
        "roll_number": profile.roll_number or "—",
        "dept": profile.dept or "—",
        "year": profile.year or "—",
        "phone": profile.phone or "—",
        "hostel_type": (profile.hostel_type or "—").capitalize(),
        "block": profile.block or "—",
        "floor": profile.floor or "—",
        "room_no": profile.room_no or "—"
    }
 
 
@app.route("/admin/update_complaint/<int:cid>", methods=["POST"])
@login_required
@admin_required
def update_complaint(cid):
    c = Complaint.query.get_or_404(cid)
    new_status = (request.form.get("status") or "").strip()
    admin_note = sanitize_description(request.form.get("admin_note", ""), 500)
 
    if new_status in VALID_STATUSES:
        c.status = new_status
    c.admin_note = admin_note
    c.updated_at = datetime.utcnow()
    db.session.commit()
    flash(f"Complaint #{cid} updated to '{c.status}'.", "success")
    next_url = request.form.get("next")
    if next_url and next_url.startswith("/"):
        return redirect(next_url)
    return redirect(url_for("welcome"))
 
 
@app.route("/admin/delete_complaint/<int:cid>", methods=["POST"])
@login_required
@admin_required
def delete_complaint(cid):
    c = Complaint.query.get_or_404(cid)
    if c.image_filename:
        img_path = os.path.join(UPLOAD_DIR, c.image_filename)
        if os.path.exists(img_path):
            try:
                os.remove(img_path)
            except OSError:
                pass
    db.session.delete(c)
    db.session.commit()
    flash(f"Complaint #{cid} deleted.", "success")
    next_url = request.form.get("next")
    if next_url and next_url.startswith("/"):
        return redirect(next_url)
    return redirect(url_for("welcome"))
 
 
import csv
import io
 
def get_floor_sort_key(floor_str):
    if not floor_str:
        return (999999, "")
    val = floor_str.lower().strip()
    
    # Check common non-numeric terms first
    if "ground" in val or "g floor" in val or "g-floor" in val:
        return (0, val)
    if "basement" in val:
        return (-1, val)
        
    # Try to extract numbers
    nums = re.findall(r'\d+', val)
    if nums:
        return (int(nums[0]), val)
        
    # Text number checks
    if "first" in val or "1st" in val:
        return (1, val)
    if "second" in val or "2nd" in val:
        return (2, val)
    if "third" in val or "3rd" in val:
        return (3, val)
    if "fourth" in val or "4th" in val:
        return (4, val)
    if "fifth" in val or "5th" in val:
        return (5, val)
        
    # Fallback default sorting
    return (9999, val)
 
@app.route("/admin/reports")
@login_required
@admin_required
def admin_reports():
    return render_template(
        "reports.html",
        email=session["user"],
        full_name=session.get("full_name", ""),
        role=session.get("role")
    )
 
@app.route("/admin/reports/download")
@login_required
@admin_required
def admin_reports_download():
    hostel_type = request.args.get("hostel_type", "").strip().lower()
    report_type = request.args.get("report_type", "").strip().lower()
    block = request.args.get("block", "").strip().upper()
    category = request.args.get("category", "").strip()
    from_date_str = request.args.get("from_date", "").strip()
    to_date_str = request.args.get("to_date", "").strip()
 
    if hostel_type not in ["boys", "girls"]:
        return "Invalid hostel type specified", 400
 
    query = Complaint.query.filter(Complaint.hostel_type == hostel_type)
 
    if report_type == "each_block_all_categories":
        if not block:
            return "Block parameter is required for this report type", 400
        query = query.filter(Complaint.block == block)
 
    elif report_type == "each_block_each_category":
        if not block or not category:
            return "Both block and category parameters are required for this report type", 400
        query = query.filter(Complaint.block == block, Complaint.category == category)
 
    elif report_type == "all_blocks_each_category":
        if not category:
            return "Category parameter is required for this report type", 400
        query = query.filter(Complaint.category == category)
 
    elif report_type == "all_blocks_all_categories":
        # No extra filters
        pass
    else:
        return "Invalid report type specified", 400
 
    if from_date_str:
        try:
            from_dt = datetime.strptime(from_date_str, "%Y-%m-%d")
            query = query.filter(Complaint.created_at >= from_dt)
        except ValueError:
            pass
 
    if to_date_str:
        try:
            to_dt = datetime.strptime(to_date_str, "%Y-%m-%d")
            to_dt = to_dt.replace(hour=23, minute=59, second=59, microsecond=999999)
            query = query.filter(Complaint.created_at <= to_dt)
        except ValueError:
            pass
 
    complaints = query.all()
    if not complaints:
        flash("No complaints registered on selected period of time", "warning")
        return redirect(url_for("admin_reports"))
 
    # Sort logic: block A to F, then each block details must be floor 1 to last
    complaints_sorted = sorted(
        complaints,
        key=lambda c: (c.block or "", get_floor_sort_key(c.floor))
    )
 
    from flask import Response
    
    def generate_csv():
        output = io.StringIO()
        writer = csv.writer(output)
        
        # Write headers
        writer.writerow([
            "Complaint ID",
            "Student Name",
            "Student Email",
            "Hostel Type",
            "Block",
            "Floor",
            "Room Number",
            "Category",
            "Priority",
            "Description",
            "Status",
            "Admin Note",
            "Created At",
            "Updated At"
        ])
        yield output.getvalue()
        output.truncate(0)
        output.seek(0)
 
        for c in complaints_sorted:
            writer.writerow([
                c.id,
                c.student_name,
                c.student_email,
                (c.hostel_type or "").capitalize(),
                c.block or "—",
                c.floor or "—",
                c.room_number,
                c.category,
                c.priority,
                c.description,
                c.status,
                c.admin_note or "—",
                c.created_at.strftime('%Y-%m-%d %H:%M:%S') if c.created_at else "—",
                c.updated_at.strftime('%Y-%m-%d %H:%M:%S') if c.updated_at else "—"
            ])
            yield output.getvalue()
            output.truncate(0)
            output.seek(0)
 
    filename_parts = [hostel_type, report_type]
    if block:
        filename_parts.append(f"block_{block}")
    if category:
        filename_parts.append(category.replace(" ", "_").lower())
    
    filename = f"hostel_report_{'_'.join(filename_parts)}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
 
    response = Response(generate_csv(), mimetype="text/csv")
    response.headers["Content-Disposition"] = f"attachment; filename={filename}"
    return response
 
 
@app.route("/admin/reports/view")
@login_required
@admin_required
def admin_reports_view():
    hostel_type = request.args.get("hostel_type", "").strip().lower()
    report_type = request.args.get("report_type", "").strip().lower()
    block = request.args.get("block", "").strip().upper()
    category = request.args.get("category", "").strip()
    from_date_str = request.args.get("from_date", "").strip()
    to_date_str = request.args.get("to_date", "").strip()
 
    if hostel_type not in ["boys", "girls"]:
        return "Invalid hostel type specified", 400
 
    query = Complaint.query.filter(Complaint.hostel_type == hostel_type)
 
    if report_type == "each_block_all_categories":
        if not block:
            return "Block parameter is required for this report type", 400
        query = query.filter(Complaint.block == block)
 
    elif report_type == "each_block_each_category":
        if not block or not category:
            return "Both block and category parameters are required for this report type", 400
        query = query.filter(Complaint.block == block, Complaint.category == category)
 
    elif report_type == "all_blocks_each_category":
        if not category:
            return "Category parameter is required for this report type", 400
        query = query.filter(Complaint.category == category)
 
    elif report_type == "all_blocks_all_categories":
        pass
    else:
        return "Invalid report type specified", 400
 
    if from_date_str:
        try:
            from_dt = datetime.strptime(from_date_str, "%Y-%m-%d")
            query = query.filter(Complaint.created_at >= from_dt)
        except ValueError:
            pass
 
    if to_date_str:
        try:
            to_dt = datetime.strptime(to_date_str, "%Y-%m-%d")
            to_dt = to_dt.replace(hour=23, minute=59, second=59, microsecond=999999)
            query = query.filter(Complaint.created_at <= to_dt)
        except ValueError:
            pass
 
    complaints = query.all()
    if not complaints:
        flash("No complaints registered on selected period of time", "warning")
        return redirect(url_for("admin_reports"))
 
    # Sort logic: block A to F, then each block details must be floor 1 to last
    complaints_sorted = sorted(
        complaints,
        key=lambda c: (c.block or "", get_floor_sort_key(c.floor))
    )
 
    scope_str = "All Blocks with All Categories"
    if report_type == "each_block_all_categories":
        scope_str = f"Block {block} with All Categories"
    elif report_type == "each_block_each_category":
        scope_str = f"Block {block} with {category}"
    elif report_type == "all_blocks_each_category":
        scope_str = f"All Blocks with {category}"
 
    return render_template(
        "report_print.html",
        complaints=complaints_sorted,
        hostel_type=hostel_type,
        scope_str=scope_str,
        generation_date=datetime.utcnow().strftime('%B %d, %Y at %I:%M %p UTC'),
        admin_name=session.get("full_name", session["user"])
    )
 
 
@app.route("/admin/wardens")
@login_required
@admin_required
def admin_wardens():
    wardens = Warden.query.all()
    return render_template(
        "wardens.html",
        email=session["user"],
        full_name=session.get("full_name", ""),
        role=session.get("role"),
        wardens=wardens
    )
 
 
@app.route("/admin/wardens/edit", methods=["POST"])
@login_required
@admin_required
def admin_wardens_edit():
    warden_id = request.form.get("warden_id")
    name = sanitize_string(request.form.get("name", ""), 120).strip()
    contact = sanitize_string(request.form.get("contact", ""), 50).strip()
 
    if not warden_id or not name or not contact:
        flash("All fields are required.", "error")
        return redirect(url_for("welcome"))
 
    warden = Warden.query.get_or_404(int(warden_id))
    warden.name = name
    warden.contact = contact
    try:
        db.session.commit()
        flash(f"Warden details for Block {warden.block} ({warden.hostel_type.capitalize()}) updated.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Failed to update warden: {str(e)}", "error")
 
    return redirect(url_for("admin_wardens"))
 
 
@app.route("/admin/notices")
@login_required
@admin_required
def admin_notices():
    notices = Notice.query.order_by(Notice.created_at.desc()).all()
    return render_template(
        "notices.html",
        email=session["user"],
        full_name=session.get("full_name", ""),
        role=session.get("role"),
        notices=notices
    )
 
 
@app.route("/admin/notices/add", methods=["POST"])
@login_required
@admin_required
def admin_notices_add():
    hostel_type = sanitize_string(request.form.get("hostel_type", ""), 10).strip().lower()
    block = sanitize_string(request.form.get("block", ""), 10).strip()
    if block.lower() == "all":
        block = "all"
    else:
        block = block.upper()
    title = sanitize_string(request.form.get("title", ""), 120).strip()
    content = sanitize_string(request.form.get("content", ""), 1000).strip()
    resolved_by_str = request.form.get("resolved_by", "").strip()
 
    if not hostel_type or not block or not title or not content:
        flash("All fields are required.", "error")
        return redirect(url_for("admin_notices"))
 
    resolved_by = None
    if resolved_by_str:
        try:
            resolved_by = datetime.strptime(resolved_by_str, "%Y-%m-%dT%H:%M")
        except ValueError:
            pass
 
    try:
        new_notice = Notice(
            hostel_type=hostel_type,
            block=block,
            title=title,
            content=content,
            resolved_by=resolved_by
        )
        db.session.add(new_notice)
        db.session.commit()
        flash("Notice added successfully.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Failed to add notice: {str(e)}", "error")
 
    return redirect(url_for("admin_notices"))
 
 
@app.route("/admin/notices/delete/<int:nid>", methods=["POST"])
@login_required
@admin_required
def admin_notices_delete(nid):
    notice = Notice.query.get_or_404(nid)
    try:
        db.session.delete(notice)
        db.session.commit()
        flash("Notice deleted successfully.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Failed to delete notice: {str(e)}", "error")
 
    return redirect(url_for("admin_notices"))
 
 
@app.route("/logout")
def logout():
 
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))
 
 
@app.errorhandler(400)
def bad_request(e):
    return render_template(
        "error.html", code=400, title="Bad Request",
        message="Your request could not be processed. This can happen if a form session expired — please go back and try again."
    ), 400


@app.errorhandler(403)
def forbidden(e):
    return render_template(
        "error.html", code=403, title="Access Denied",
        message="You don't have permission to view this page. If you believe this is a mistake, please contact the hostel administration."
    ), 403


@app.errorhandler(404)
def not_found(e):
    return render_template(
        "error.html", code=404, title="Page Not Found",
        message="The page you're looking for doesn't exist or may have been moved."
    ), 404


@app.errorhandler(500)
def server_error(e):
    db.session.rollback()
    return render_template(
        "error.html", code=500, title="Something Went Wrong",
        message="An unexpected server error occurred. Please try again in a moment."
    ), 500
 
 
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))