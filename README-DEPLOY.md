# SPARK Desk — Hostel Complaint Management System

A Flask-based complaint management system for SECE hostels (boys & girls, Blocks A–F).
Students raise and track complaints; wardens/admins triage, update status, and
generate reports.

## Features

- Email/password login and Google OAuth (`@sece.ac.in` accounts only)
- Student self-registration with hostel/block/room details
- Complaint submission with photo upload and automatic priority
  classification (keyword-based fallback, optional Gemini AI classification)
- Role-based dashboards (student vs admin) with charts, block/hostel analytics
- Admin complaint triage: status updates, admin notes, deletion
- Warden directory management per block
- Hostel-wide notices/announcements targeted by hostel type and block
- CSV report generation and printable report view, filterable by hostel,
  block, category, and date range
- CSRF-protected forms, security headers, session-based auth
- `/health` endpoint reporting live database connectivity for uptime checks

## Local Setup

1. **Create a virtual environment and install dependencies:**
   ```bash
   python3 -m venv venv
   source venv/bin/activate        # Windows: venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. **Configure environment variables.** Copy `.env.example` to `.env` and fill
   in the values you need:
   ```bash
   cp .env.example .env
   ```

   | Variable | Required | Purpose |
   |---|---|---|
   | `SECRET_KEY` | Recommended | Flask session/CSRF signing key. A random key is auto-generated if omitted, but sessions won't survive a restart. |
   | `DATABASE_URL` | No | PostgreSQL connection string. Falls back to a local SQLite file (`app.sqlite3`) if unset. |
   | `ADMIN_PASSWORD` | No | Password for the built-in `admin@sece.ac.in` account. Defaults to `Admin@1234` — **change this in production**. |
   | `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | No | Enables "Continue with Google" sign-in. Without these, Google sign-in is disabled gracefully (falls back to email/password). |
   | `GEMINI_API_KEY` | No | Enables AI-assisted complaint priority classification. Without it, a keyword-based fallback is used automatically. |

3. **Run the app:**
   ```bash
   python app.py
   ```
   The app starts on `http://localhost:5000`. The database schema and default
   seed data (wardens, notices) are created automatically on first request.

4. **Log in as admin:** `admin@sece.ac.in` / the value of `ADMIN_PASSWORD`
   (default `Admin@1234`).

## Production Deployment

### Gunicorn (any VM / container)
```bash
gunicorn wsgi:app --bind 0.0.0.0:8000 --workers 3 --timeout 30 --preload
```
(See `Procfile` for the equivalent command using Render's `$PORT`.) The
`--preload` flag matters here: it loads the app once in the master process
*before* forking workers, so the startup database initialization (schema
creation + seed data) runs exactly once instead of once per worker — without
it, multiple workers can race and insert duplicate warden/notice rows.

### Render
1. Create a Web Service from this repo.
2. **Start Command:** `gunicorn wsgi:app --bind 0.0.0.0:$PORT --workers 3 --timeout 30 --preload`
3. Set the environment variables below (Environment tab) — in particular
   `DATABASE_URL` must be your Supabase **Session Pooler** connection string,
   not the direct `db.<project-ref>.supabase.co` host (that host is
   IPv6-only and Render cannot reach it — this previously caused a
   `Network is unreachable` error at startup).
4. Render automatically sets a `RENDER` environment variable on every
   deployed service. The app uses this to require `DATABASE_URL` be set in
   production — it will refuse to silently fall back to SQLite there (SQLite
   is for local development only).
5. Python version is pinned via `.python-version` to avoid Render defaulting
   to an untested/too-new Python release that may lack prebuilt wheels for
   `psycopg2-binary`.
6. After deploying, check `/health` — it returns
   `{"status": "healthy", "database": "connected"}` (200) when the database
   is reachable, or `{"status": "unhealthy", ...}` (503) if not, without
   affecting static assets or other pages.

### Vercel
This project ships with a `vercel.json` configured for the Python runtime.
Push the repository to Vercel and set the environment variables above in the
project's dashboard. Note: Vercel's filesystem is read-only except `/tmp`,
so uploaded complaint photos are stored under `/tmp/uploads` there — this is
**ephemeral** storage. For durable photo storage in production, connect an
object store (S3, Cloudinary, Vercel Blob) and swap the local `UPLOAD_DIR`
logic in `app.py` for uploads to that service.

### Database
- SQLite is fine for local development/demos but does not persist on most
  serverless platforms — set `DATABASE_URL` to a managed PostgreSQL instance
  (e.g. Neon, Supabase, RDS) for production.
- Schema migrations are handled with lightweight in-app `ALTER TABLE`
  checks on startup — no separate migration step is required.
- **Render + Supabase:** Supabase's direct database host
  (`db.<project-ref>.supabase.co`) resolves to an IPv6-only address, which
  Render's network cannot reach — this causes
  `OperationalError: ... Network is unreachable`. Use Supabase's **Session
  Pooler** connection string instead (Supabase dashboard → Connect → Session
  Pooler), which is IPv4-compatible, and set that as `DATABASE_URL` in
  Render's Environment Variables. See `.env.example` for the expected format.

## Project Structure
```
spark_desk/
├── app.py                # Flask app, models, routes
├── wsgi.py                # WSGI entrypoint (gunicorn / Vercel)
├── requirements.txt
├── vercel.json
├── Procfile               # gunicorn start command (Render/Heroku-style)
├── .python-version        # pinned Python runtime for Render
├── .env.example
├── static/
│   ├── SECElogo.png
│   ├── hostelimage.png
│   └── uploads/           # complaint photos (gitignored)
└── templates/
    ├── login.html          # login + signup
    ├── welcome.html        # role-based dashboard
    ├── complaint.html      # complaint submission form
    ├── reports.html        # admin report builder
    ├── report_print.html   # printable/exportable report
    ├── wardens.html        # warden directory (admin)
    ├── notices.html        # notices management (admin)
    └── error.html          # 400/403/404/500 pages
```

## Default Accounts (development seed data)
- **Admin:** `admin@sece.ac.in` / `Admin@1234` (or your `ADMIN_PASSWORD`)
- Students self-register via the Sign Up form using a `@sece.ac.in` email.
