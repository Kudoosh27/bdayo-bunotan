# Production deployment guide

This guide covers a single-instance Flask deployment using SQLite. Ask the
district's Data Protection Officer to review the privacy notice, consent
process, retention schedule, and operational access before using real
participant data.

## GitHub is not the application host

GitHub is suitable for versioning the source, but GitHub Pages cannot run this
Flask application or persist its SQLite database and private packets. Keep
participant data, `.env` files, database files, backups, and generated packets
out of the repository. For a live site, use a Python-capable host that provides
HTTPS and a persistent local disk, and follow the single-instance requirements
below.

### Render setup

The reported build completed successfully. The service exited because its
Start Command was `gunicorn.conf.py`, which is a configuration file, not a
command. In Render's **Settings → Build & Deploy**, set:

- **Root Directory:** the directory containing `requirements.txt` (if the
  project files are in the repository root, leave this blank).
- **Build Command:** `pip install -r requirements.txt`
- **Start Command:** `gunicorn --config gunicorn.conf.py 'app:create_app()'`

The Gunicorn configuration reads Render's `PORT` environment variable and
binds to `0.0.0.0`. The repository's `.python-version` requests Python 3.12.
In Render **Environment**, set `SECRET_KEY` to a long randomly generated
secret, `APP_ENV` to `production`, and `SESSION_COOKIE_SECURE` to `1`.

This app must use a **persistent disk** for SQLite and private packet files.
Attach a Render disk mounted at `/var/data`, then set:

```text
DATABASE_URL=sqlite:////var/data/bunotan.db
PRIVATE_STORAGE_DIR=/var/data/private
```

Use one web-service instance and do not enable autoscaling or multiple worker
processes. SQLite on the disk is not suitable for a horizontally scaled
deployment. If your Render service plan does not support persistent disks,
stop here rather than using the ephemeral filesystem for participant data.
After the first deploy, use Render Shell to run
`flask --app app:create_app init-db` and `flask --app app:create_app
create-admin` with the same environment. `seed-demo --confirm-demo` is for
testing only.

The local print assets and templates need to be included in the repository.
Check the deployment after launch: test registration, login, a page reload,
and packet generation; restart or redeploy and confirm data still exists.
Do not use real participant data until persistent storage, HTTPS, and backups
have been confirmed.

### Vercel

Vercel supports Flask as a Python Function. This repository includes `index.py`
and a `pyproject.toml` entrypoint setting so Vercel can locate the WSGI app.
Set `SECRET_KEY` in Vercel's environment settings before deploying. However,
this app is **not ready for real data on Vercel**: its SQLite database and
private packet files require persistent local storage, and its static assets
are served from `app/static/`. Deploying the function alone does not provide
the storage and static-file behavior this app expects. Use a host with a
persistent local disk, or redesign the database, private-file, and static-asset
storage before using Vercel.

## 1. Host and application

Use a maintained Linux host, Python 3.12, a dedicated operating-system account,
and a local filesystem with reliable backups. Keep the application database
and private packet directory on storage that is not served as static content.

```bash
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Set configuration in the service manager or secret manager. `.env.example` is
a reference only; the application does not require or depend on a `.env` file.

| Variable | Production guidance |
| --- | --- |
| `SECRET_KEY` | Supply a long random secret through a protected secret manager. Never commit or log it. |
| `DATABASE_URL` | Use the default `sqlite:///instance/bunotan.db` unless a protected local path is needed. Do not put the database on a network filesystem. |
| `APP_ENV` | Set to `production`; production also enables secure session cookies. |
| `SESSION_COOKIE_SECURE` | Set to `1` when HTTPS is enabled. |
| `PRIVATE_STORAGE_DIR` | Optional protected directory outside `app/static/`; restrict it to the service account. |
| `LOOKUP_MAX_ATTEMPTS` | Choose a reasonable lockout threshold; default is 5. |
| `LOOKUP_LOCK_MINUTES` | Choose a reasonable lockout duration; default is 15. |
| `PRIVACY_NOTICE_VERSION` | Set this to the district-approved privacy notice version. |
| `PUBLIC_CONTACT_EMAIL` | Set a monitored privacy/help contact for participants. |
| `GUNICORN_BIND` | Optional bind override; defaults to `0.0.0.0:$PORT` (or port 8000 locally). |
| `GUNICORN_TRUSTED_PROXIES` | Trust only the proxy addresses that set forwarding headers. |
| `GUNICORN_THREADS` | A small thread count is sufficient; the default is 4. |

Initialize the database and create the first administrator as a release
operation, not from each web worker:

```bash
flask --app app:create_app init-db
flask --app app:create_app create-admin
```

The administrator command prompts for the password. Do not put passwords in
command arguments, scripts, or source. `seed-demo --confirm-demo` is for
explicitly confirmed non-production databases only.

The application stores rate-limit counters in SQLite and relies on SQLite
locking and transactions for draw atomicity. Run **one Gunicorn worker process
and one application instance**; do not scale this app horizontally or run
multiple Gunicorn workers against the same SQLite database. A few Gunicorn
threads are supported.

## 2. HTTPS and Gunicorn

Terminate TLS at a maintained reverse proxy. Use a valid certificate, redirect
HTTP to HTTPS, and configure current TLS versions. The proxy should set
forwarding headers and remove untrusted inbound values. Bind Gunicorn to
loopback or a private interface; do not expose its port publicly.

Run the included configuration:

```bash
gunicorn --config gunicorn.conf.py 'app:create_app()'
```

The configuration uses one `gthread` worker and four threads by default. Keep
the application to one instance; do not override the worker count. Send logs
to the platform logging service with restricted access. Never log passwords,
PINs, participant assignment contents, or packet HTML.

## 3. SQLite backups and recovery

The default database is `instance/bunotan.db`. Before the centralized drawing,
create and verify a protected backup. Also back up before maintenance. Use
SQLite's online backup API (which handles WAL correctly) or stop the
application and create a consistent backup. For example, with Python's
standard library:

```bash
python -c "import sqlite3; source=sqlite3.connect('instance/bunotan.db'); target=sqlite3.connect('instance/bunotan-backup.db'); source.backup(target); target.close(); source.close()"
```

Store backups encrypted, access-controlled, and off-host. Test restoration to
an isolated environment regularly. Protect the private packet directory as
confidential participant data, and back it up only if the district's retention
policy requires it. Database rows contain only PIN hashes; generated packet
HTML contains readable recipient and PIN details and cannot be regenerated
from those hashes alone.

Do not delete or replace a live database while the app is running. SQLite WAL
mode may create `-wal` and `-shm` sidecar files; use the SQLite backup API
rather than copying only the main database file while writes are active.

## 4. Permissions, credentials, and retention

Restrict the application tree, database, backups, logs, and private storage to
the service and designated operators. Do not place participant exports,
packets, or backups below `app/static/` or a web server document root.

Legacy Argon2 hashes cannot be verified after this migration. Run
`reset-all-credentials --confirm-reset` during a controlled maintenance window.
It prompts for new staff passwords, rotates participant references and PINs,
voids old distribution batches, and creates a private one-time credential CSV.
Deliver that file securely and remove it after delivery.

Have the district DPO approve a written retention schedule. The application
does not automatically delete registrations, audit rows, packet HTML, exports,
or backups. Assign an owner to perform and record retention tasks for the
database, private files, downloaded CSVs, paper copies, replicas, and backups.

Never distribute assignment exports or print packets to church leaders or
participants. Public participants can retrieve only their own recipient after
reference and PIN verification.
