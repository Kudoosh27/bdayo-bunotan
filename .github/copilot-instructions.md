# Copilot instructions

## Stack and project structure

- Use Python 3.12, Flask app factory and blueprints, Flask-SQLAlchemy, and
  SQLite. Frontend code is HTML/Jinja, CSS, and vanilla JavaScript; Chart.js,
  Lucide, and Inter are self-hosted under `app/static/`.
- The supported runtime/test packages are Flask, Flask-SQLAlchemy, Gunicorn,
  and pytest. Do not add PostgreSQL drivers, migrations, Flask-Login,
  Flask-WTF, Flask-Limiter, Argon2, Bootstrap, jQuery, spreadsheet libraries,
  or PDF-rendering packages.
- Keep route handlers thin. Put domain behavior in `app/services/`, and reuse
  authorization helpers in `app/services/access.py`.
- Initialize a new database with `flask init-db`. Schema changes must remain
  compatible with SQLite and be tested with the existing in-memory SQLite
  fixtures; do not introduce Alembic or Flask-Migrate.

## Privacy and authorization rules

- Assignments are visible only to district administrators, except that a
  participant may retrieve their own recipient after reference and PIN
  verification. Church leaders are church-scoped and must never receive
  assignment data.
- Store passwords and PINs only as Werkzeug scrypt hashes. Use constant-time
  verification and keep generic credential failure messages.
- Do not bypass custom signed-session authentication, CSRF validation, or
  database-backed rate limiting. Every POST form must include the CSRF token.
- Imported participants remain pending; only administrators modify official
  records. Audit sensitive administrative and distribution actions.
- Do not expose private print packets or credential exports under `static/`.
  Keep Jinja autoescaping enabled and never apply `|safe` to user data.
- Collect only necessary personal information and preserve privacy and guardian
  consent behavior.

## UI and verification

- Preserve the deep navy, warm cream, muted gold, and evergreen theme, accessible
  responsive layouts, and light/dark mode. Do not add Bootstrap classes or
  third-party runtime assets.
- Imports and exports are UTF-8 CSV only, with the fixed headers and BOM
  behavior already used by the app. Distribution packets are admin-authorized
  print-optimized HTML.
- Add focused pytest coverage for every behavior change. Run the smallest
  relevant tests while iterating, then run `python -m pytest -q` before
  considering a change complete. Do not claim untested behavior works.
