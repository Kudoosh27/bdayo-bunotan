# District Youth Secret Bunotan

District-wide secret gift exchange registration and distribution application.
Built for Python 3.12 with Flask, Flask-SQLAlchemy, SQLite, server-rendered
HTML, custom CSS, vanilla JavaScript, and locally hosted Chart.js, Lucide, and
Inter assets.

## Run locally on Windows

Open PowerShell in the project folder (the folder containing `requirements.txt`).
Install Python 3.12 first if it is not already available, then run:

```powershell
py -3.12 --version
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$secret = python -c "import secrets; print(secrets.token_hex(32))"
$env:SECRET_KEY = $secret.Trim()
python -m flask --app app:create_app init-db
python -m flask --app app:create_app seed-demo --confirm-demo
python -m flask --app app:create_app create-admin
python -m flask --app app:create_app run --debug
```

Keep that PowerShell window open while testing. Visit
<http://127.0.0.1:5000/> in your browser and sign in at `/login` with the admin
credentials you just created. Stop the server with **Ctrl+C**. The generated
database is `instance/bunotan.db`; the `instance/` directory is git-ignored.
Demo data is synthetic and clearly labeled. Do not use it as real participant
data.

To remove only the seeded DEMO event and its related records after testing, run:

```powershell
python -m flask --app app:create_app clear-demo --confirm-clear
```

This also removes private packet files for that demo draw, but keeps user
accounts and unrelated events or participants. A DEMO church still used by
another participant, leader, or packet is retained. The command does nothing
without `--confirm-clear`.

If PowerShell blocks `Activate.ps1`, either allow local scripts for your user
with `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, or run the virtual
environment's interpreter directly instead of activating it:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$secret = & .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_hex(32))"
$env:SECRET_KEY = $secret.Trim()
.\.venv\Scripts\python.exe -m flask --app app:create_app init-db
.\.venv\Scripts\python.exe -m flask --app app:create_app seed-demo --confirm-demo
.\.venv\Scripts\python.exe -m flask --app app:create_app create-admin
.\.venv\Scripts\python.exe -m flask --app app:create_app run --debug
```

In that case, run tests with
`.\.venv\Scripts\python.exe -m pytest -q` from the project folder.
`create-admin` prompts for a username and hidden, confirmed password.
Passwords and retrieval PINs are hashed with Werkzeug's scrypt support.
`DATABASE_URL` defaults to `sqlite:///instance/bunotan.db`. For real use, set
a stable, secret `SECRET_KEY` in the process environment and replace demo data
with the district's actual event setup.

Add leader accounts with
`python -m flask --app app:create_app create-user --role leader --church-id <UUID>`.
Leaders are scoped to one church; only district administrators can review
records, access assignments, or manage distribution.

## GitHub and live hosting

GitHub can store and version this project's source code, but **GitHub Pages
cannot run this Flask application**. Pages only serves static files; this app
needs a running Python server, SQLite database, and private storage for print
packets. You can push the source to a GitHub repository, then deploy the
application separately to a host that supports Flask/Gunicorn and a persistent
local disk. Follow [DEPLOYMENT.md](./DEPLOYMENT.md) before making it publicly
available.

To publish the source, first create an empty repository on GitHub, then run
these commands from the project folder. Review `git status` carefully before
committing:

```powershell
git init
git add .
git status --short
git commit -m "Initial Bunotan source"
git branch -M main
git remote add origin https://github.com/<YOUR-USERNAME>/<REPOSITORY>.git
git push -u origin main
```

Before pushing, make sure your `.env`, `.venv/`, `instance/`, database backups,
and private packet/credential files are not included. Do not upload real
participant data to GitHub. Keep the application to one instance with a
persistent disk, HTTPS, restricted file permissions, and tested backups.

## Registration and privacy

The public pages include event mechanics, gift guidelines, FAQs, the privacy
notice, contact information, and registration. Public registration is open
only while an event is in `registration_open`. New submissions remain pending
until an administrator reviews them. The form records participation
confirmation, privacy consent, and guardian consent for minors.

Participants receive a reference and PIN after registration. PINs are stored
as scrypt hashes in SQLite. Public lookup uses generic failures, database-backed
rate limits, attempt lockout, and access logging; only the verified participant
can view their own recipient. Church leaders can see only their church's
submission status and never assignments.

Leaders may submit one participant or import CSV using the fixed headers
`full_name,is_minor,guardian_consent`. Files must be UTF-8 CSV and may contain
up to 500 rows. The portal provides a row-level preview, validation errors,
duplicate warnings, and an error-report download. Imports are always pending;
they are never auto-approved. The downloadable template includes a UTF-8 BOM.

## Drawing and distribution

Administrators manage churches, leader access, participant review, event
settings and lifecycle transitions. A centralized validated draw assigns all
approved participants without self-assignments. Withdrawing someone after a
draw requires a reason, supersedes the whole draw, and invalidates packets from
the superseded version.

After finalization, administrators can generate private, print-optimized HTML
packets. Each page has the giver on its outside label and the recipient,
reference, and PIN inside a confidential insert. Use the page's **Print / Save
as PDF** control to print or save a PDF in the browser. Packets and individual
cards are stored outside `static/` and served only through administrator
authorization; generation, print recording, handoff, acknowledgment,
replacement, invalidation, and report exports are audited.

Administrators can export masterlists, church summaries, drawing validation,
distribution status, withdrawals, audit data, and the confidential assignment
list as UTF-8 CSV. Assignment exports are restricted to administrators. Handle
all exports and printed packets as confidential data.

## Operations and credential reset

The schema is initialized with `flask init-db` (`db.create_all` plus a
`schema_version` row); this application does not use migrations. Take a
verified SQLite backup before the drawing and before maintenance. SQLite
foreign keys and WAL mode are enabled on database connections.

Existing Argon2 hashes cannot be verified with the target stack. After moving
an existing database to this version, run
`flask --app app:create_app reset-all-credentials --confirm-reset`. The command
prompts for every staff password, rotates participant references and PINs,
invalidates old packets, audits the reset, and writes a private one-time
credential CSV. Deliver that file securely and remove it after delivery.

See [DEPLOYMENT.md](./DEPLOYMENT.md) for HTTPS, SQLite backup, Gunicorn,
permissions, and retention guidance. Set `PUBLIC_CONTACT_EMAIL` to show the
district's monitored privacy/help contact on public pages. Review the privacy
notice, retention period, and guardian-consent process with the district's
Data Protection Officer before using real participant data.
