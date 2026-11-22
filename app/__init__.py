import os
import uuid

from flask import Flask, g, session
from sqlalchemy import event as sqlalchemy_event

from .auth_state import AnonymousUser, current_user
from .config import Config
from .csrf import csrf_token, validate_csrf_request
from .models import User, db


def create_app(config=None):
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.makedirs(os.path.join(project_root, "instance"), exist_ok=True)
    app = Flask(__name__, instance_path=project_root)
    app.config.from_object(Config)
    if config:
        app.config.update(config)
    if not app.config.get("SECRET_KEY"):
        raise RuntimeError("SECRET_KEY must be configured through the environment or app config")

    db.init_app(app)
    with app.app_context():
        engine = db.engine

        @sqlalchemy_event.listens_for(engine, "connect")
        def enable_sqlite_pragmas(connection, _connection_record):
            if engine.dialect.name != "sqlite":
                return
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    @app.before_request
    def load_session_user():
        user = AnonymousUser()
        user_id = session.get("user_id")
        if user_id:
            try:
                parsed_user_id = uuid.UUID(user_id)
            except (ValueError, TypeError):
                session.pop("user_id", None)
            else:
                loaded_user = db.session.get(User, parsed_user_id)
                if loaded_user and loaded_user.active:
                    user = loaded_user
                else:
                    session.pop("user_id", None)
        g.current_user = user

    app.before_request(validate_csrf_request)
    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.context_processor
    def inject_current_user():
        return {"current_user": current_user}

    @app.after_request
    def prevent_sensitive_response_caching(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    from .routes import admin, auth, leader, public

    app.register_blueprint(public.bp)
    app.register_blueprint(auth.bp)
    app.register_blueprint(leader.bp, url_prefix="/leader")
    app.register_blueprint(admin.bp, url_prefix="/admin")
    _register_cli(app)

    return app


def _register_cli(app):
    import click
    from .models import (
        Assignment,
        AuditLog,
        Church,
        Draw,
        Event,
        Participant,
        RegistrationStatusHistory,
        RetrievalCredential,
        SchemaVersion,
        User,
    )

    @app.cli.command("init-db")
    def init_db():
        db.create_all()
        if db.session.get(SchemaVersion, 1) is None:
            db.session.add(SchemaVersion(id=1, version=1))
            db.session.commit()
        click.echo("SQLite database initialized (schema version 1).")

    def create_account(username, role, church_id, password):
        if not username.strip() or len(username.strip()) > 120:
            raise click.ClickException("Username must contain 1 to 120 characters")
        if len(password) < 12:
            raise click.ClickException("Password must be at least 12 characters")
        if role == "leader" and not church_id:
            raise click.ClickException("--church-id is required for a leader")
        if role == "admin" and church_id:
            raise click.ClickException("Administrators cannot be church-scoped")
        try:
            church_uuid = uuid.UUID(church_id) if church_id else None
        except ValueError as exc:
            raise click.ClickException("Invalid church ID") from exc
        church = db.session.get(Church, church_uuid) if church_uuid else None
        if role == "leader" and not church:
            raise click.ClickException("Church not found")
        try:
            user = User(
                username=username.strip(),
                role=role,
                church_id=church.id if church else None,
            )
            user.set_password(password)
            db.session.add(user)
            db.session.flush()
            db.session.add(
                AuditLog(
                    action="user_created",
                    entity_type="user",
                    entity_id=str(user.id),
                    details={"role": role, "church_id": str(church.id) if church else None},
                )
            )
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            raise click.ClickException("Could not create user; username may already exist") from exc
        click.echo(f"{role.capitalize()} account created.")

    @app.cli.command("create-admin")
    @click.option("--username", prompt=True)
    def create_admin(username):
        password = click.prompt("Password", hide_input=True, confirmation_prompt=True)
        create_account(username, "admin", None, password)

    @app.cli.command("create-user")
    @click.option("--username", prompt=True)
    @click.option("--role", type=click.Choice(["admin", "leader"]), prompt=True)
    @click.option("--church-id")
    def create_user(username, role, church_id):
        password = click.prompt("Password", hide_input=True, confirmation_prompt=True)
        create_account(username, role, church_id, password)

    @app.cli.command("bootstrap-demo-admin")
    def bootstrap_demo_admin():
        if os.environ.get("BUNOTAN_DEMO_ADMIN_BOOTSTRAP") != "1":
            raise click.ClickException(
                "Demo admin bootstrap is disabled. Set "
                "BUNOTAN_DEMO_ADMIN_BOOTSTRAP=1 only for a disposable demo."
            )
        username = os.environ.get("BUNOTAN_DEMO_ADMIN_USERNAME", "").strip()
        password = os.environ.get("BUNOTAN_DEMO_ADMIN_PASSWORD", "")
        if not username or not password:
            raise click.ClickException(
                "Set BUNOTAN_DEMO_ADMIN_USERNAME and "
                "BUNOTAN_DEMO_ADMIN_PASSWORD in the environment."
            )
        if len(password) < 12:
            raise click.ClickException(
                "The demo admin password must be at least 12 characters."
            )
        existing = User.query.filter_by(username=username).first()
        if existing:
            if existing.role != "admin":
                raise click.ClickException(
                    "The configured demo username is already used by a non-admin account."
                )
            click.echo("Demo admin already exists; no changes made.")
            return
        create_account(username, "admin", None, password)
        click.echo("Disposable demo admin created.")

    @app.cli.command("reset-all-credentials")
    @click.option(
        "--confirm-reset",
        is_flag=True,
        help="Confirm password, participant credential, and packet resets.",
    )
    def reset_all_credentials_command(confirm_reset):
        if not confirm_reset:
            raise click.ClickException(
                "This command resets all staff passwords and participant credentials. "
                "Re-run with --confirm-reset."
            )
        users = User.query.order_by(User.username).all()
        passwords = {}
        for index, user in enumerate(users, start=1):
            password = click.prompt(
                f"New password for staff account {index} of {len(users)}",
                hide_input=True,
                confirmation_prompt=True,
            )
            if len(password) < 12:
                raise click.ClickException("Passwords must be at least 12 characters")
            passwords[str(user.id)] = password

        from .services.credential_reset import (
            CredentialResetError,
            reset_all_credentials,
        )

        try:
            export_path = reset_all_credentials(passwords)
        except CredentialResetError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(
            "All staff passwords and participant retrieval credentials were reset. "
            f"Deliver the private participant credential export securely: {export_path}"
        )

    @app.cli.command("seed-demo")
    @click.option(
        "--confirm-demo",
        is_flag=True,
        help="Confirm that synthetic DEMO records should be added to this database.",
    )
    def seed_demo(confirm_demo):
        if not confirm_demo:
            raise click.ClickException(
                "This command adds synthetic DEMO records. Re-run with --confirm-demo."
            )
        event_name = "DEMO - District Youth Bunotan Sample"
        if Event.query.filter_by(name=event_name).first():
            click.echo("DEMO data already exists; no changes made.")
            return

        from .services.pins import hash_pin, new_pin, new_reference

        event = Event(name=event_name, status="distribution")
        churches = [
            Church(name=f"DEMO - {name}")
            for name in ("North Church", "Central Church", "South Church")
        ]
        db.session.add_all([event, *churches])
        db.session.flush()
        participants = []
        specifications = [
            ("DEMO Participant 01", churches[0], "approved"),
            ("DEMO Participant 02", churches[0], "approved"),
            ("DEMO Participant 03", churches[1], "approved"),
            ("DEMO Participant 04", churches[2], "approved"),
            ("DEMO Participant Withdrawn", churches[2], "withdrawn"),
        ]
        for name, church, status in specifications:
            participant = Participant(
                event_id=event.id,
                church_id=church.id,
                full_name=name,
                name_key=name.casefold(),
                status=status,
                participation_confirmed=True,
                privacy_consent=True,
            )
            db.session.add(participant)
            db.session.flush()
            participants.append(participant)
            db.session.add(
                RetrievalCredential(
                    participant_id=participant.id,
                    reference=new_reference(),
                    pin_hash=hash_pin(new_pin()),
                    revoked_at=(
                        db.func.now() if status == "withdrawn" else None
                    ),
                )
            )
            if status == "withdrawn":
                db.session.add(
                    RegistrationStatusHistory(
                        participant_id=participant.id,
                        previous_status="approved",
                        new_status="withdrawn",
                        reason="DEMO synthetic withdrawal",
                    )
                )

        approved = [person for person in participants if person.status == "approved"]
        draw = Draw(
            event_id=event.id,
            version=1,
            participant_count=len(approved),
            status="finalized",
            validation_ok=True,
        )
        db.session.add(draw)
        db.session.flush()
        db.session.add_all(
            Assignment(
                draw_id=draw.id,
                giver_id=person.id,
                recipient_id=approved[(index + 1) % len(approved)].id,
            )
            for index, person in enumerate(approved)
        )
        db.session.add(
            AuditLog(
                action="demo_data_seeded",
                entity_type="event",
                entity_id=str(event.id),
                details={
                    "event_name": event_name,
                    "church_count": len(churches),
                    "participant_count": len(participants),
                    "synthetic_data_only": True,
                },
            )
        )
        try:
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            raise click.ClickException("Could not create DEMO data.") from exc
        click.echo(
            f"Created clearly marked DEMO event '{event_name}' with "
            f"{len(churches)} churches and {len(participants)} synthetic participants. "
            "No user accounts were created."
        )

    @app.cli.command("clear-demo")
    @click.option(
        "--confirm-clear",
        is_flag=True,
        help="Confirm deletion of the seeded DEMO event and its related records.",
    )
    def clear_demo(confirm_clear):
        if not confirm_clear:
            raise click.ClickException(
                "This removes the seeded DEMO event and its related records. "
                "Re-run with --confirm-clear."
            )
        from .services.demo_data import DemoCleanupError, clear_demo_data

        try:
            result = clear_demo_data()
        except DemoCleanupError as exc:
            raise click.ClickException(str(exc)) from exc
        if not result["found"]:
            click.echo("No seeded DEMO event was found; no changes made.")
            return
        counts = ", ".join(
            f"{count} {name.replace('_', ' ')}"
            for name, count in result["deleted"].items()
        )
        click.echo(f"Cleared seeded DEMO data ({counts}).")
        if result["retained_churches"]:
            click.echo(
                "Kept DEMO churches still used by other participants, leaders, "
                "or packets: " + ", ".join(result["retained_churches"])
            )
