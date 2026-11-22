import csv
import io
import re
import uuid
from datetime import datetime, timezone

import pytest
from flask.testing import FlaskClient
from app import create_app
from app.models import (
    Assignment,
    AssignmentAccessLog,
    AuditLog,
    Church,
    DistributionBatch,
    DistributionRecord,
    Draw,
    Event,
    Participant,
    RegistrationStatusHistory,
    RateLimitAttempt,
    RetrievalCredential,
    SchemaVersion,
    User,
    db,
)
from app.services.draw_service import cancel_draw, draw_preflight, withdraw_after_draw
from app.services.pins import hash_pin, verify_pin
from app.services.rate_limits import rate_limit_key
from app.services.participants import parse_import_file
from app.services.access import can_access_church
from werkzeug.datastructures import FileStorage
from click.testing import CliRunner


class CSRFTestClient(FlaskClient):
    auto_csrf = True

    def post(self, *args, **kwargs):
        data = kwargs.get("data")
        if self.auto_csrf and not (
            hasattr(data, "get") and data.get("csrf_token")
        ):
            with self.session_transaction() as signed_session:
                token = signed_session.get("csrf_token")
            if not token:
                self.get("/login")
                with self.session_transaction() as signed_session:
                    token = signed_session.get("csrf_token")
            data = data.copy() if hasattr(data, "copy") else dict(data or {})
            data["csrf_token"] = token
            kwargs["data"] = data
        return super().post(*args, **kwargs)


@pytest.fixture
def app():
    application = create_app(
        {
            "SECRET_KEY": "test-secret",
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite://",
            "LOOKUP_MAX_ATTEMPTS": 2,
        }
    )
    application.test_client_class = CSRFTestClient
    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def test_app_requires_configured_secret_key():
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_app({"SECRET_KEY": None})


def test_init_db_creates_schema_version_and_enables_sqlite_foreign_keys(app):
    result = CliRunner().invoke(app.cli, ["init-db"])
    assert result.exit_code == 0, result.output
    with app.app_context():
        version = db.session.get(SchemaVersion, 1)
        assert version is not None
        assert version.version == 1
        connection = db.engine.raw_connection()
        try:
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys")
            assert cursor.fetchone()[0] == 1
            cursor.close()
        finally:
            connection.close()


def test_home_uses_self_hosted_frontend_assets(client):
    response = client.get("/")
    assert response.status_code == 200
    assert b"css/app.css" in response.data
    assert b"vendor/lucide.min.js" in response.data
    assert b"How it works" in response.data
    assert client.get("/static/vendor/bootstrap.min.css").status_code == 404
    for asset in (
        "/static/vendor/chart.umd.min.js",
        "/static/vendor/lucide.min.js",
    ):
        assert client.get(asset).status_code == 200
    font = client.get("/static/fonts/Inter-Regular.ttf")
    assert font.status_code == 200
    assert len(font.data) > 100_000


@pytest.mark.parametrize(
    "path",
    ["/mechanics", "/gift-guidelines", "/faq", "/privacy", "/contact"],
)
def test_public_information_pages_render(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert b"District Youth Bunotan" in response.data


def test_registration_requires_session_csrf_token():
    application = create_app(
        {
            "SECRET_KEY": "csrf-test-secret",
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite://",
        }
    )
    application.test_client_class = CSRFTestClient
    with application.app_context():
        db.create_all()
        event, church, _ = _seed_event()
        event_id, church_id = str(event.id), str(church.id)
    client = application.test_client()
    client.auto_csrf = False
    form = client.get("/register")
    assert form.status_code == 200
    data = {
        "full_name": "CSRF Participant",
        "event_id": event_id,
        "church_id": church_id,
        "participation_confirmed": "yes",
        "privacy_consent": "yes",
    }
    assert client.post("/register", data=data).status_code == 400
    csrf_token = re.search(
        rb'name="csrf_token" value="([^"]+)"', form.data
    ).group(1).decode()
    response = client.post("/register", data={**data, "csrf_token": csrf_token})
    assert response.status_code == 200
    assert b"Your one-time lookup details" in response.data


def test_registration_rate_limit_counts_submissions_not_page_views():
    application = create_app(
        {
            "SECRET_KEY": "rate-limit-test-secret",
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite://",
        }
    )
    application.test_client_class = CSRFTestClient
    with application.app_context():
        db.create_all()
        event, church, _ = _seed_event()
        event_id, church_id = str(event.id), str(church.id)
    client = application.test_client()
    for _ in range(120):
        assert client.get(
            "/register", environ_overrides={"REMOTE_ADDR": "192.0.2.77"}
        ).status_code == 200
    with application.app_context():
        assert (
            db.session.get(
                RateLimitAttempt, rate_limit_key("registration", "192.0.2.77")
            )
            is None
        )

    for index in range(100):
        response = client.post(
            "/register",
            data={
                "event_id": event_id,
                "church_id": church_id,
                "full_name": f"Rate Limited Registrant {index}",
                "participation_confirmed": "yes",
                "privacy_consent": "yes",
            },
            environ_overrides={"REMOTE_ADDR": "192.0.2.77"},
        )
        assert response.status_code == 200
        assert b"Your one-time lookup details" in response.data
    assert client.post(
        "/register",
        data={
            "event_id": event_id,
            "church_id": church_id,
            "full_name": "Rate Limited Registrant 101",
            "participation_confirmed": "yes",
            "privacy_consent": "yes",
        },
        environ_overrides={"REMOTE_ADDR": "192.0.2.77"},
    ).status_code == 429
    with application.app_context():
        attempt = db.session.get(
            RateLimitAttempt, rate_limit_key("registration", "192.0.2.77")
        )
        assert attempt.count == 100
        assert attempt.locked_until is not None
        assert Participant.query.filter_by(event_id=uuid.UUID(event_id)).count() == 100


def test_login_lockout_is_persisted_per_ip(app, client):
    for _ in range(4):
        response = client.post(
            "/login", data={"username": "not-an-account", "password": "wrong"}
        )
        assert response.status_code == 200
    assert client.post(
        "/login", data={"username": "not-an-account", "password": "wrong"}
    ).status_code == 429
    with app.app_context():
        attempt = db.session.get(RateLimitAttempt, rate_limit_key("login", "127.0.0.1"))
        assert attempt.count == 5
        assert attempt.locked_until is not None


def _seed_event(status="registration_open", people=()):
    event = Event(name="District Exchange", status=status)
    church = Church(name="North Church")
    db.session.add_all([event, church])
    db.session.flush()
    participants = []
    for name in people:
        participant = Participant(
            event_id=event.id,
            church_id=church.id,
            full_name=name,
            name_key=name.casefold(),
            status="approved",
            participation_confirmed=True,
            privacy_consent=True,
        )
        db.session.add(participant)
        db.session.flush()
        _add_credential(participant)
        participants.append(participant)
    db.session.commit()
    return event, church, participants


def _add_credential(participant, reference=None, pin="282828"):
    credential = RetrievalCredential(
        participant_id=participant.id,
        reference=reference or f"SB-{uuid.uuid4().hex[:8].upper()}",
        pin_hash=hash_pin(pin),
    )
    db.session.add(credential)
    return credential


def _login(client, username, password):
    return client.post(
        "/login", data={"username": username, "password": password}, follow_redirects=True
    )


def test_public_registration_creates_pending_and_shows_pin_once(app, client):
    with app.app_context():
        event, church, _ = _seed_event()
        event_id, church_id = str(event.id), str(church.id)
    response = client.post(
        "/register",
        data={
            "full_name": "Ari Santos",
            "event_id": event_id,
            "church_id": church_id,
            "privacy_consent": "yes",
            "participation_confirmed": "yes",
            "gift_preference": "  books   and art ",
        },
        follow_redirects=True,
    )
    text = response.get_data(as_text=True)
    assert response.headers["Cache-Control"] == "no-store"
    reference = re.search(r"<dd>(SB-[A-F0-9]+)</dd>", text).group(1)
    pin = re.search(r"<dd>(\d{6})</dd>", text).group(1)
    with app.app_context():
        credential = RetrievalCredential.query.filter_by(reference=reference).one()
        participant = db.session.get(Participant, credential.participant_id)
        assert participant.status == "pending"
        assert participant.participation_confirmed is True
        assert participant.gift_preference == "books and art"
        assert participant.guardian_consent is False
        assert credential.pin_hash != pin
        assert verify_pin(credential.pin_hash, pin)
        assert participant.privacy_consent
        submission_log = AuditLog.query.filter_by(action="participant_submitted").one()
        assert submission_log.entity_id == str(participant.id)
        assert RegistrationStatusHistory.query.filter_by(
            participant_id=participant.id, new_status="pending"
        ).count() == 1
    second_response = client.get("/register")
    assert pin not in second_response.get_data(as_text=True)


def test_minor_registration_requires_guardian_consent(app, client):
    with app.app_context():
        event, church, _ = _seed_event()
        event_id, church_id = str(event.id), str(church.id)
    response = client.post(
        "/register",
        data={
            "full_name": "Minor Participant",
            "event_id": event_id,
            "church_id": church_id,
            "is_minor": "yes",
            "privacy_consent": "yes",
            "participation_confirmed": "yes",
        },
    )
    assert b"Guardian consent is required" in response.data
    with app.app_context():
        assert Participant.query.count() == 0


def test_duplicate_registration_is_rejected_case_insensitively(app, client):
    with app.app_context():
        event, church, _ = _seed_event()
        event_id, church_id = str(event.id), str(church.id)
    registration = {
        "full_name": "  Ari   Santos ",
        "event_id": event_id,
        "church_id": church_id,
        "privacy_consent": "yes",
        "participation_confirmed": "yes",
    }
    first = client.post("/register", data=registration)
    assert first.status_code == 200
    assert b"Your one-time lookup details" in first.data
    second = client.post(
        "/register",
        data={**registration, "full_name": "ari santos"},
    )
    assert second.status_code == 200
    assert b"already submitted for this church" in second.data
    assert b"Private PIN" not in second.data
    with app.app_context():
        assert Participant.query.filter_by(event_id=event.id).count() == 1
        assert RetrievalCredential.query.count() == 1


def test_lookup_reveals_only_own_recipient_in_distribution(app, client):
    with app.app_context():
        event, church, people = _seed_event("distribution", ["Giver", "My Recipient", "Other Person"])
        draw = Draw(event_id=event.id, version=1, participant_count=3, validation_ok=True)
        db.session.add(draw)
        db.session.flush()
        db.session.add_all(
            [
                Assignment(draw_id=draw.id, giver_id=people[0].id, recipient_id=people[1].id),
                Assignment(draw_id=draw.id, giver_id=people[1].id, recipient_id=people[2].id),
                Assignment(draw_id=draw.id, giver_id=people[2].id, recipient_id=people[0].id),
            ]
        )
        reference = people[0].retrieval_credential.reference
        participant_id = people[0].id
        db.session.commit()
    response = client.post("/lookup", data={"reference": reference, "pin": "282828"})
    assert b"My Recipient" in response.data
    assert b"Other Person" not in response.data
    assert b"Giver" not in response.data


def test_lookup_allows_repeat_retrieval_and_logs_each_access(app, client):
    with app.app_context():
        event, church, people = _seed_event(
            "distribution", ["Repeat Giver", "Repeat Recipient"]
        )
        draw = Draw(
            event_id=event.id, version=1, participant_count=2, validation_ok=True
        )
        db.session.add(draw)
        db.session.flush()
        db.session.add_all(
            [
                Assignment(
                    draw_id=draw.id,
                    giver_id=people[0].id,
                    recipient_id=people[1].id,
                ),
                Assignment(
                    draw_id=draw.id,
                    giver_id=people[1].id,
                    recipient_id=people[0].id,
                ),
            ]
        )
        reference = people[0].retrieval_credential.reference
        participant_id = people[0].id
        db.session.commit()
    first = client.post("/lookup", data={"reference": reference, "pin": "282828"})
    second = client.post("/lookup", data={"reference": reference, "pin": "282828"})
    assert b"Repeat Recipient" in first.data
    assert b"Repeat Recipient" in second.data
    assert b"could not verify" not in second.data
    with app.app_context():
        assert AssignmentAccessLog.query.filter_by(
            participant_id=participant_id
        ).count() == 2
        assert db.session.get(
            RateLimitAttempt, rate_limit_key("lookup-reference", reference)
        ) is None
        assert AuditLog.query.filter_by(action="recipient_lookup_succeeded").count() == 2


def test_lookup_wrong_pin_lockout_uses_generic_response_for_unknown_references(app, client):
    with app.app_context():
        _seed_event("distribution", ["Known Giver", "Known Recipient"])
    unknown = client.post(
        "/lookup", data={"reference": "SB-UNKNOWN", "pin": "123456"}
    )
    wrong = client.post(
        "/lookup", data={"reference": "SB-UNKNOWN", "pin": "654321"}
    )
    assert b"could not verify those details or the result is not available" in unknown.data
    assert b"could not verify those details or the result is not available" in wrong.data
    assert b"Unknown" not in wrong.data
    with app.app_context():
        attempt = db.session.get(
            RateLimitAttempt, rate_limit_key("lookup-reference", "SB-UNKNOWN")
        )
        assert attempt.count == 2
        assert attempt.locked_until is not None
        assert AssignmentAccessLog.query.count() == 0


def test_lookup_lockout_blocks_correct_pin_until_expiry(app, client):
    with app.app_context():
        event, church, people = _seed_event("distribution", ["Giver", "Recipient"])
        draw = Draw(event_id=event.id, version=1, participant_count=2, validation_ok=True)
        db.session.add(draw)
        db.session.flush()
        db.session.add_all(
            [
                Assignment(draw_id=draw.id, giver_id=people[0].id, recipient_id=people[1].id),
                Assignment(draw_id=draw.id, giver_id=people[1].id, recipient_id=people[0].id),
            ]
        )
        reference = people[0].retrieval_credential.reference
        db.session.commit()
    client.post("/lookup", data={"reference": reference, "pin": "000000"})
    client.post("/lookup", data={"reference": reference, "pin": "000000"})
    response = client.post("/lookup", data={"reference": reference, "pin": "282828"})
    assert b"Recipient" not in response.data
    with app.app_context():
        attempt = db.session.get(
            RateLimitAttempt, rate_limit_key("lookup-reference", reference)
        )
        assert attempt.locked_until is not None


def test_leader_sees_only_own_church_and_cannot_access_admin(app, client):
    with app.app_context():
        event, own_church, _ = _seed_event()
        other_church = Church(name="South Church")
        db.session.add(other_church)
        db.session.flush()
        own = Participant(
            event_id=event.id,
            church_id=own_church.id,
            full_name="Own Participant",
            name_key="own participant",
            status="pending",
        )
        other = Participant(
            event_id=event.id,
            church_id=other_church.id,
            full_name="Private Other",
            name_key="private other",
            status="pending",
        )
        db.session.add_all([own, other])
        db.session.flush()
        _add_credential(own, "SB-OWN0001")
        _add_credential(other, "SB-OTHR0001")
        leader = User(username="leader", role="leader", church_id=own_church.id)
        leader.set_password("leader-password")
        db.session.add_all([own, other, leader])
        db.session.commit()
    _login(client, "leader", "leader-password")
    response = client.get("/leader/")
    assert b"Own Participant" in response.data
    assert b"Private Other" not in response.data
    assert client.get("/admin/").status_code == 403


def test_leader_import_preview_skips_errors_and_duplicates_and_submits_pending(app, client):
    with app.app_context():
        event, church, _ = _seed_event()
        other_church = Church(name="South Church")
        db.session.add(other_church)
        db.session.flush()
        db.session.add_all(
            [
                Participant(
                    event_id=event.id,
                    church_id=church.id,
                    full_name="Existing Youth",
                    name_key="existing youth",
                    status="pending",
                ),
                Participant(
                    event_id=event.id,
                    church_id=other_church.id,
                    full_name="Cross Church Youth",
                    name_key="cross church youth",
                    status="pending",
                ),
            ]
        )
        leader = User(username="importer", role="leader", church_id=church.id)
        leader.set_password("leader-password")
        db.session.add(leader)
        db.session.commit()
        event_id, church_id, other_church_id = (
            str(event.id),
            str(church.id),
            str(other_church.id),
        )
    _login(client, "importer", "leader-password")
    content = (
        "full_name,is_minor,guardian_consent\n"
        "Cross Church Youth,no,no\n"
        "Existing Youth,no,no\n"
        "Minor Without Consent,yes,no\n"
        "Repeat Youth,no,no\n"
        "repeat youth,no,no\n"
    )
    data = {
        "event_id": event_id,
        "church_id": other_church_id,
        "privacy_consent": "yes",
        "participation_confirmed": "yes",
        "file": (io.BytesIO(content.encode()), "participants.csv"),
    }
    preview = client.post(
        "/leader/import/preview",
        data=data,
        headers={"X-Requested-With": "XMLHttpRequest"},
        content_type="multipart/form-data",
    )
    assert preview.status_code == 200
    preview_data = preview.get_json()
    assert preview_data["eligible_count"] == 2
    assert preview_data["error_count"] == 1
    assert preview_data["duplicate_count"] == 2
    assert "credentials" not in preview_data
    with app.app_context():
        assert Participant.query.filter_by(full_name="Cross Church Youth").count() == 1
        assert AuditLog.query.filter_by(action="leader_import_previewed").count() == 1

    report = client.post(
        "/leader/import/error-report",
        data={
            "event_id": event_id,
            "church_id": other_church_id,
            "privacy_consent": "yes",
            "participation_confirmed": "yes",
            "file": (io.BytesIO(content.encode()), "participants.csv"),
        },
        content_type="multipart/form-data",
    )
    assert report.status_code == 200
    assert report.data.startswith(b"\xef\xbb\xbf")
    assert "attachment;" in report.headers["Content-Disposition"]
    assert b"duplicate_warning" in report.data
    assert b"Guardian consent is required" in report.data
    with app.app_context():
        assert AuditLog.query.filter_by(
            action="leader_import_report_downloaded"
        ).count() == 1

    report_audit = client.post(
        "/leader/import/report-audit",
        data={"event_id": event_id, "issue_count": "3"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert report_audit.status_code == 204
    with app.app_context():
        assert AuditLog.query.filter_by(
            action="leader_import_report_downloaded"
        ).count() == 2

    imported = client.post(
        "/leader/import/confirm",
        data={
            "event_id": event_id,
            "church_id": other_church_id,
            "privacy_consent": "yes",
            "participation_confirmed": "yes",
            "file": (io.BytesIO(content.encode()), "participants.csv"),
        },
        headers={"X-Requested-With": "XMLHttpRequest"},
        content_type="multipart/form-data",
    )
    assert imported.status_code == 200
    result = imported.get_json()
    assert result["eligible_count"] == 0
    assert len(result["credentials"]) == 2
    assert "submitted as pending" in result["message"]
    with app.app_context():
        own_church_participants = Participant.query.filter_by(
            church_id=uuid.UUID(church_id)
        ).all()
        assert {person.full_name for person in own_church_participants} == {
            "Existing Youth",
            "Cross Church Youth",
            "Repeat Youth",
        }
        assert all(person.status == "pending" for person in own_church_participants)
        imported_participants = Participant.query.filter(
            Participant.church_id == uuid.UUID(church_id),
            Participant.full_name.in_(["Cross Church Youth", "Repeat Youth"]),
        ).all()
        assert all(
            person.church_id == uuid.UUID(church_id) for person in imported_participants
        )
        assert all(person.retrieval_credential.pin_hash != "282828" for person in imported_participants)
        assert AuditLog.query.filter_by(action="leader_import_completed").one().details[
            "submitted_count"
        ] == 2


def test_csv_import_parser_reads_fixed_headers_and_utf8_bom():
    upload = FileStorage(
        stream=io.BytesIO(
            "\ufefffull_name,is_minor,guardian_consent\nCSV Youth,yes,yes\n".encode(
                "utf-8"
            )
        ),
        filename="participants.csv",
    )
    assert parse_import_file(upload) == [
        {
            "row_number": 2,
            "full_name": "CSV Youth",
            "is_minor": "yes",
            "guardian_consent": "yes",
            "source_errors": [],
        }
    ]
    with pytest.raises(ValueError, match="Upload a CSV file"):
        parse_import_file(
            FileStorage(stream=io.BytesIO(b"not a spreadsheet"), filename="participants.xlsx")
        )


def test_leader_downloads_fixed_templates_and_printable_form(app, client):
    with app.app_context():
        _, church, _ = _seed_event()
        leader = User(username="forms-leader", role="leader", church_id=church.id)
        leader.set_password("leader-password")
        db.session.add(leader)
        db.session.commit()
        church_name = church.name
    _login(client, "forms-leader", "leader-password")

    csv_template = client.get("/leader/import-template/csv")
    assert csv_template.status_code == 200
    assert csv_template.data.startswith(b"\xef\xbb\xbf")
    assert csv_template.data.decode("utf-8-sig").strip() == (
        "full_name,is_minor,guardian_consent"
    )
    assert "attachment;" in csv_template.headers["Content-Disposition"]
    assert client.get("/leader/import-template/xlsx").status_code == 404

    paper_form = client.get("/leader/paper-registration")
    assert paper_form.status_code == 200
    assert church_name.encode() in paper_form.data
    assert b"window.print()" in paper_form.data
    assert b"Guardian consent" in paper_form.data


def test_leader_import_has_html_preview_fallback(app, client):
    with app.app_context():
        event, church, _ = _seed_event()
        leader = User(username="fallback-leader", role="leader", church_id=church.id)
        leader.set_password("leader-password")
        db.session.add(leader)
        db.session.commit()
        event_id = str(event.id)
    _login(client, "fallback-leader", "leader-password")
    response = client.post(
        "/leader/import/preview",
        data={
            "event_id": event_id,
            "privacy_consent": "yes",
            "participation_confirmed": "yes",
            "file": (
                io.BytesIO(
                    b"full_name,is_minor,guardian_consent\nFallback Youth,no,no\n"
                ),
                "participants.csv",
            ),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    assert b"Import preview" in response.data
    assert b"Select the same CSV file again to confirm" in response.data
    with app.app_context():
        assert Participant.query.filter_by(full_name="Fallback Youth").count() == 0


def test_leader_cannot_see_assignments_or_other_church_recipient(app, client):
    with app.app_context():
        event, church, _ = _seed_event()
        other_church = Church(name="South Church")
        db.session.add(other_church)
        db.session.flush()
        giver = Participant(
            event_id=event.id,
            church_id=church.id,
            full_name="Leader Church Giver",
            name_key="leader church giver",
            status="approved",
        )
        recipient = Participant(
            event_id=event.id,
            church_id=other_church.id,
            full_name="Hidden Other Church Recipient",
            name_key="hidden other church recipient",
            status="approved",
        )
        draw = Draw(
            event_id=event.id,
            version=1,
            participant_count=2,
            validation_ok=True,
        )
        leader = User(username="assignment-leader", role="leader", church_id=church.id)
        leader.set_password("leader-password")
        db.session.add_all([other_church, giver, recipient, draw, leader])
        db.session.flush()
        db.session.add(
            Assignment(
                draw_id=draw.id,
                giver_id=giver.id,
                recipient_id=recipient.id,
            )
        )
        db.session.commit()
    _login(client, "assignment-leader", "leader-password")
    dashboard = client.get("/leader/")
    assert b"Leader Church Giver" in dashboard.data
    assert b"Hidden Other Church Recipient" not in dashboard.data
    assert client.get("/leader/assignments").status_code == 404


def test_admin_approves_only_during_verification_and_audits(app, client):
    with app.app_context():
        event, church, _ = _seed_event("verification")
        participant = Participant(
            event_id=event.id,
            church_id=church.id,
            full_name="Pending Youth",
            name_key="pending youth",
            status="pending",
        )
        db.session.add(participant)
        db.session.flush()
        _add_credential(participant, "SB-PEND0001")
        admin = User(username="admin", role="admin")
        admin.set_password("district-password")
        db.session.add_all([participant, admin])
        db.session.commit()
        participant_id, admin_id = participant.id, admin.id
    _login(client, "admin", "district-password")
    response = client.post(
        f"/admin/participants/{participant_id}/review",
        data={"status": "approved"},
    )
    assert response.status_code == 302
    with app.app_context():
        participant = db.session.get(Participant, participant_id)
        review_log = AuditLog.query.filter_by(action="participant_reviewed").one()
        assert participant.status == "approved"
        assert review_log.actor_id == admin_id
        history = RegistrationStatusHistory.query.filter_by(
            participant_id=participant_id, new_status="approved"
        ).one()
        assert history.previous_status == "pending"
        assert history.changed_by_id == admin_id
    client.post(
        f"/admin/participants/{participant_id}/review",
        data={"status": "rejected"},
    )
    with app.app_context():
        assert db.session.get(Participant, participant_id).status == "approved"


def test_admin_dashboard_metrics_and_navigation_hide_assignments(app, client):
    with app.app_context():
        event, church, _ = _seed_event("registration_open", ["Metrics Youth"])
        pending = Participant(
            event_id=event.id,
            church_id=church.id,
            full_name="Waiting Youth",
            name_key="waiting youth",
            status="pending",
        )
        under_review = Participant(
            event_id=event.id,
            church_id=church.id,
            full_name="Being Reviewed",
            name_key="being reviewed",
            status="under_review",
        )
        admin = User(username="metrics-admin", role="admin")
        admin.set_password("district-password")
        db.session.add_all([pending, under_review, admin])
        db.session.commit()
        event_id = str(event.id)
    _login(client, "metrics-admin", "district-password")
    response = client.get("/admin/")
    text = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Administration navigation" in text
    assert "admin-nav-overlay" in text
    assert "Active churches" in text
    assert "Awaiting review" in text
    assert "Participants" in text
    assert "Manage churches" in text
    assert "Awaiting review</span><strong>2</strong>" in text
    assert "Registrations by church" in text
    assert "Registration statuses" in text
    assert "Distribution progress" in text
    assert "vendor/chart.umd.min.js" in text
    assert "North Church" in text
    assert "Assignment data is not displayed" in text
    assert "Hidden Recipient" not in text
    participants = client.get(f"/admin/events/{event_id}/participants")
    assert "recipient_id" not in participants.get_data(as_text=True)
    review_queue = client.get("/admin/participants?status=awaiting_review")
    assert b"Waiting Youth" in review_queue.data
    assert b"Being Reviewed" in review_queue.data
    assert b"Metrics Youth" not in review_queue.data


def test_admin_church_management_and_leader_assignment_are_audited(app, client):
    with app.app_context():
        _, church, _ = _seed_event()
        leader = User(username="managed-leader", role="leader", church_id=church.id)
        leader.set_password("leader-password")
        admin = User(username="church-admin", role="admin")
        admin.set_password("district-password")
        db.session.add_all([leader, admin])
        db.session.commit()
        church_id, leader_id = church.id, leader.id
    _login(client, "church-admin", "district-password")
    create = client.post("/admin/churches", data={"name": "South Fellowship"})
    assert create.status_code == 302
    with app.app_context():
        new_church = Church.query.filter_by(name="South Fellowship").one()
        new_church_id = str(new_church.id)
        assert AuditLog.query.filter_by(action="church_created").count() == 1

    assigned = client.post(
        f"/admin/users/{leader_id}/access",
        data={"church_id": new_church_id, "active": "yes"},
    )
    assert assigned.status_code == 302
    with app.app_context():
        leader = db.session.get(User, leader_id)
        assert leader.church_id == uuid.UUID(new_church_id)
        assert leader.active is True
        assert AuditLog.query.filter_by(action="user_access_updated").count() == 1

    deactivated = client.post(
        f"/admin/churches/{new_church_id}/update",
        data={"name": "South Fellowship"},
    )
    assert deactivated.status_code == 302
    with app.app_context():
        church = db.session.get(Church, uuid.UUID(new_church_id))
        leader = db.session.get(User, leader_id)
        assert church.active is False
        assert leader.active is False
        assert AuditLog.query.filter_by(action="church_updated").one().details[
            "leaders_disabled"
        ] == 1

    assert client.get("/admin/churches").status_code == 200
    assert client.get("/admin/users").status_code == 200
    with app.app_context():
        original_church = db.session.get(Church, church_id)
        assert original_church.active is True


def test_admin_can_create_church_leader_with_scoped_access_and_audit(app, client):
    with app.app_context():
        _, church, _ = _seed_event()
        admin = User(username="leader-create-admin", role="admin")
        admin.set_password("district-password")
        db.session.add(admin)
        db.session.commit()
        church_id = str(church.id)
    _login(client, "leader-create-admin", "district-password")

    page = client.get("/admin/users")
    assert b"Add a church leader" in page.data
    response = client.post(
        "/admin/users",
        data={
            "username": "new-church-leader",
            "church_id": church_id,
            "password": "temporary-password-123",
            "password_confirmation": "temporary-password-123",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Church leader account created" in response.data
    with app.app_context():
        leader = User.query.filter_by(username="new-church-leader").one()
        assert leader.role == "leader"
        assert str(leader.church_id) == church_id
        assert leader.active
        assert leader.check_password("temporary-password-123")
        assert "temporary-password-123" not in leader.password_hash
        creation_audit = AuditLog.query.filter_by(action="user_created").one()
        assert creation_audit.details == {
            "role": "leader",
            "church_id": church_id,
            "source": "admin_ui",
        }
        assert "temporary-password-123" not in str(creation_audit.details)


def test_admin_leader_creation_rejects_bad_password_and_duplicate_username(app, client):
    with app.app_context():
        _, church, _ = _seed_event()
        existing = User(username="already-used-leader", role="leader", church_id=church.id)
        existing.set_password("existing-password")
        admin = User(username="leader-validation-admin", role="admin")
        admin.set_password("district-password")
        db.session.add_all([existing, admin])
        db.session.commit()
        church_id = str(church.id)
    _login(client, "leader-validation-admin", "district-password")

    short_password = client.post(
        "/admin/users",
        data={
            "username": "short-password-leader",
            "church_id": church_id,
            "password": "short",
            "password_confirmation": "short",
        },
        follow_redirects=True,
    )
    assert b"at least 12 characters" in short_password.data
    mismatch = client.post(
        "/admin/users",
        data={
            "username": "mismatch-leader",
            "church_id": church_id,
            "password": "a-long-password-123",
            "password_confirmation": "a-different-password-123",
        },
        follow_redirects=True,
    )
    assert b"confirmation does not match" in mismatch.data
    duplicate = client.post(
        "/admin/users",
        data={
            "username": "already-used-leader",
            "church_id": church_id,
            "password": "a-long-password-123",
            "password_confirmation": "a-long-password-123",
        },
        follow_redirects=True,
    )
    assert b"username is already in use" in duplicate.data
    with app.app_context():
        assert User.query.count() == 2
        assert AuditLog.query.filter_by(action="leader_creation_rejected").count() == 3


def test_leader_cannot_create_staff_accounts(app, client):
    with app.app_context():
        _, church, _ = _seed_event()
        leader = User(username="staff-create-leader", role="leader", church_id=church.id)
        leader.set_password("leader-password")
        db.session.add(leader)
        db.session.commit()
        church_id = str(church.id)
    _login(client, "staff-create-leader", "leader-password")
    response = client.post(
        "/admin/users",
        data={
            "username": "unauthorized-new-leader",
            "church_id": church_id,
            "password": "a-long-password-123",
            "password_confirmation": "a-long-password-123",
        },
    )
    assert response.status_code == 403
    with app.app_context():
        assert User.query.filter_by(username="unauthorized-new-leader").count() == 0


def test_admin_participant_filters_correction_duplicate_guard_and_withdrawal(app, client):
    with app.app_context():
        event, church, people = _seed_event("registration_open", ["Ari Santos", "Bea Santos"])
        admin = User(username="participant-admin", role="admin")
        admin.set_password("district-password")
        db.session.add(admin)
        db.session.commit()
        event_id, church_id, participant_id = event.id, church.id, people[0].id
    _login(client, "participant-admin", "district-password")

    filtered = client.get(
        f"/admin/participants?event_id={event_id}&church_id={church_id}"
        "&status=approved&q=Ari"
    )
    assert b"Ari Santos" in filtered.data
    assert b"Bea Santos" not in filtered.data

    corrected = client.post(
        f"/admin/participants/{participant_id}/correct",
        data={
            "full_name": "Ari S.",
            "gift_preference": "Art supplies",
            "reason": "Participant corrected spelling",
            "return_to": f"/admin/participants?event_id={event_id}",
        },
        follow_redirects=True,
    )
    assert corrected.status_code == 200
    assert b"Participant details corrected" in corrected.data
    with app.app_context():
        participant = db.session.get(Participant, participant_id)
        assert participant.full_name == "Ari S."
        assert participant.name_key == "ari s."
        assert participant.gift_preference == "Art supplies"
        correction = AuditLog.query.filter_by(action="participant_corrected").one()
        assert correction.details["reason"] == "Participant corrected spelling"

    duplicate_correction = client.post(
        f"/admin/participants/{participant_id}/correct",
        data={
            "full_name": "Bea Santos",
            "gift_preference": "",
            "reason": "Mistaken duplicate",
        },
        follow_redirects=True,
    )
    assert b"already uses that name" in duplicate_correction.data
    with app.app_context():
        participant = db.session.get(Participant, participant_id)
        assert participant.full_name == "Ari S."
        assert AuditLog.query.filter_by(
            action="participant_correction_rejected"
        ).count() == 1

    withdrawn = client.post(
        f"/admin/events/{event_id}/participants/{participant_id}/withdraw",
        data={"reason": "Participant opted out"},
        follow_redirects=True,
    )
    assert b"Participant withdrawn" in withdrawn.data
    with app.app_context():
        participant = db.session.get(Participant, participant_id)
        assert participant.status == "withdrawn"
        assert participant.retrieval_credential.revoked_at is not None
        assert RegistrationStatusHistory.query.filter_by(
            participant_id=participant_id, new_status="withdrawn"
        ).one().reason == "Participant opted out"
        assert AuditLog.query.filter_by(action="participant_withdrawn").one()


def test_admin_corrections_are_blocked_after_finalized_draw(app, client):
    with app.app_context():
        event, church, people = _seed_event(
            "drawing_finalized", ["Locked Youth", "Other Youth"]
        )
        draw = Draw(
            event_id=event.id,
            version=1,
            participant_count=2,
            validation_ok=True,
        )
        admin = User(username="locked-admin", role="admin")
        admin.set_password("district-password")
        db.session.add_all([draw, admin])
        db.session.commit()
        participant_id, event_id = people[0].id, event.id
    _login(client, "locked-admin", "district-password")
    response = client.post(
        f"/admin/participants/{participant_id}/correct",
        data={
            "full_name": "Changed Name",
            "gift_preference": "",
            "reason": "Spelling",
        },
        follow_redirects=True,
    )
    assert b"cannot be corrected after the draw is finalized" in response.data
    with app.app_context():
        assert db.session.get(Participant, participant_id).full_name == "Locked Youth"
        assert AuditLog.query.filter_by(
            action="participant_correction_rejected"
        ).one()


def test_admin_duplicate_report_renders_cleanly_when_no_duplicates(app, client):
    with app.app_context():
        _seed_event("registration_open", ["Unique Youth"])
        admin = User(username="duplicates-admin", role="admin")
        admin.set_password("district-password")
        db.session.add(admin)
        db.session.commit()
    _login(client, "duplicates-admin", "district-password")
    response = client.get("/admin/duplicates")
    assert response.status_code == 200
    assert b"No active duplicate records were found" in response.data
    assert b"Active duplicate records" in client.get("/admin/").data


def test_admin_duplicate_report_finds_legacy_duplicate_groups(app, client):
    with app.app_context():
        event, church, _ = _seed_event("registration_open", ["Legacy Duplicate"])
        db.session.execute(db.text("DROP INDEX uq_active_participant"))
        duplicate = Participant(
            event_id=event.id,
            church_id=church.id,
            full_name=" legacy   duplicate ",
            name_key="legacy duplicate",
            status="pending",
        )
        admin = User(username="legacy-duplicates-admin", role="admin")
        admin.set_password("district-password")
        db.session.add_all([duplicate, admin])
        db.session.commit()
        event_id, church_id = event.id, church.id
    _login(client, "legacy-duplicates-admin", "district-password")
    response = client.get("/admin/duplicates")
    assert response.status_code == 200
    assert b"North Church" in response.data
    assert b"Legacy Duplicate" in response.data
    filtered = client.get("/admin/participants?duplicates=1")
    assert b"Legacy Duplicate" in filtered.data
    assert b"legacy   duplicate" in filtered.data
    dashboard = client.get("/admin/")
    assert b"Active duplicate records</span><strong>2</strong>" in dashboard.data
    event_page = client.get(
        f"/admin/participants?event_id={event_id}&church_id={church_id}&duplicates=1"
    )
    assert event_page.status_code == 200


def test_admin_event_settings_and_lifecycle_require_review_and_valid_draw(app, client):
    with app.app_context():
        event, church, _ = _seed_event("verification")
        pending = Participant(
            event_id=event.id,
            church_id=church.id,
            full_name="Needs Review",
            name_key="needs review",
            status="pending",
        )
        approved = [
            Participant(
                event_id=event.id,
                church_id=church.id,
                full_name=name,
                name_key=name.casefold(),
                status="approved",
                participation_confirmed=True,
                privacy_consent=True,
            )
            for name in ("Ready One", "Ready Two")
        ]
        admin = User(username="lifecycle-admin", role="admin")
        admin.set_password("district-password")
        db.session.add_all([pending, *approved, admin])
        db.session.flush()
        for participant in approved:
            _add_credential(participant)
        db.session.commit()
        event_id = event.id
    _login(client, "lifecycle-admin", "district-password")
    settings = client.post(
        f"/admin/events/{event_id}/settings",
        data={"name": "District Bunotan 2026", "gift_budget": "250.50"},
    )
    assert settings.status_code == 302
    with app.app_context():
        event = db.session.get(Event, event_id)
        assert event.name == "District Bunotan 2026"
        assert str(event.gift_budget) == "250.50"
        assert AuditLog.query.filter_by(action="event_settings_updated").one()

    invalid_budget = client.post(
        f"/admin/events/{event_id}/settings",
        data={"name": "District Bunotan 2026", "gift_budget": "-1"},
        follow_redirects=True,
    )
    assert b"Gift budget must be between" in invalid_budget.data

    blocked = client.post(
        f"/admin/events/{event_id}/transition",
        data={"status": "ready_for_drawing"},
        follow_redirects=True,
    )
    assert b"Resolve every pending registration" in blocked.data
    with app.app_context():
        assert db.session.get(Event, event_id).status == "verification"
        pending = Participant.query.filter_by(full_name="Needs Review").one()
        pending.status = "rejected"
        db.session.commit()

    ready = client.post(
        f"/admin/events/{event_id}/transition",
        data={"status": "ready_for_drawing"},
    )
    assert ready.status_code == 302
    finalized = client.post(
        f"/admin/events/{event_id}/draw",
        data={"pre_draw_confirmed": "yes"},
    )
    assert finalized.status_code == 302
    distribution = client.post(
        f"/admin/events/{event_id}/transition",
        data={"status": "distribution"},
        follow_redirects=True,
    )
    assert b"Event status updated" in distribution.data
    with app.app_context():
        event = db.session.get(Event, event_id)
        assert event.status == "distribution"
        assert AuditLog.query.filter_by(action="event_status_changed").count() == 2
        assert AuditLog.query.filter_by(action="event_status_change_rejected").one()


def test_admin_cannot_complete_event_with_pending_distribution_records(app, client):
    with app.app_context():
        event, church, people = _seed_event("distribution", ["Deliver One", "Deliver Two"])
        admin = User(username="distribution-admin", role="admin")
        admin.set_password("district-password")
        db.session.add(admin)
        db.session.flush()
        draw = Draw(
            event_id=event.id,
            version=1,
            participant_count=2,
            validation_ok=True,
        )
        db.session.add(draw)
        db.session.flush()
        assignment = Assignment(
            draw_id=draw.id,
            giver_id=people[0].id,
            recipient_id=people[1].id,
        )
        reverse_assignment = Assignment(
            draw_id=draw.id,
            giver_id=people[1].id,
            recipient_id=people[0].id,
        )
        db.session.add_all([assignment, reverse_assignment])
        db.session.flush()
        batch = DistributionBatch(
            draw_id=draw.id,
            church_id=church.id,
            created_by_id=admin.id,
        )
        db.session.add(batch)
        db.session.flush()
        db.session.add(
            DistributionRecord(batch_id=batch.id, assignment_id=assignment.id)
        )
        db.session.commit()
        event_id, distribution_record_id = event.id, db.session.query(
            DistributionRecord.id
        ).scalar()
    _login(client, "distribution-admin", "district-password")
    blocked = client.post(
        f"/admin/events/{event_id}/transition",
        data={"status": "completed"},
        follow_redirects=True,
    )
    assert b"Resolve every pending distribution record" in blocked.data
    with app.app_context():
        assert db.session.get(Event, event_id).status == "distribution"
        distribution_record = db.session.get(
            DistributionRecord, distribution_record_id
        )
        distribution_record.status = "returned"
        db.session.commit()
    completed = client.post(
        f"/admin/events/{event_id}/transition",
        data={"status": "completed"},
    )
    assert completed.status_code == 302
    with app.app_context():
        assert db.session.get(Event, event_id).status == "completed"
        assert AuditLog.query.filter_by(action="event_status_change_rejected").one()
        assert AuditLog.query.filter_by(action="event_status_changed").one()


def test_admin_draw_checklist_confirmation_lock_and_supersede(app, client):
    with app.app_context():
        event, church, people = _seed_event(
            "ready_for_drawing", ["Draw One", "Draw Two", "Draw Three"]
        )
        admin = User(username="draw-admin", role="admin")
        admin.set_password("district-password")
        db.session.add(admin)
        db.session.commit()
        event_id = event.id
    _login(client, "draw-admin", "district-password")

    checklist = client.get(f"/admin/events/{event_id}/draw")
    assert checklist.status_code == 200
    assert b"Pre-draw checklist" in checklist.data
    assert b"Validation report" in checklist.data
    assert b"Every approved participant has retrieval credentials" in checklist.data
    assert b"recipient_id" not in checklist.data

    missing_confirmation = client.post(
        f"/admin/events/{event_id}/draw",
        data={},
    )
    assert missing_confirmation.status_code == 400
    with app.app_context():
        assert Draw.query.filter_by(event_id=event_id).count() == 0
        assert AuditLog.query.filter_by(
            action="draw_finalization_rejected"
        ).one().details["reason_code"] == "pre_draw_checklist_not_confirmed"

    finalized = client.post(
        f"/admin/events/{event_id}/draw",
        data={"pre_draw_confirmed": "yes"},
        follow_redirects=True,
    )
    assert b"District draw version 1 was finalized" in finalized.data
    with app.app_context():
        event = db.session.get(Event, event_id)
        first = Draw.query.filter_by(event_id=event_id).one()
        assert event.status == "drawing_finalized"
        assert first.status == "finalized"
        first_id = first.id

    locked = client.post(
        f"/admin/events/{event_id}/draw",
        data={"pre_draw_confirmed": "yes"},
        follow_redirects=True,
    )
    assert b"locked after finalization" in locked.data
    with app.app_context():
        assert Draw.query.filter_by(event_id=event_id).count() == 1

    reason = "An administrator found an error in the approved roster"
    canceled = client.post(
        f"/admin/events/{event_id}/draw/cancel",
        data={"confirm_supersede": "yes", "reason": reason},
        follow_redirects=True,
    )
    assert b"was superseded" in canceled.data
    with app.app_context():
        event = db.session.get(Event, event_id)
        first = db.session.get(Draw, first_id)
        assert event.status == "ready_for_drawing"
        assert first.status == "superseded"
        assert first.cancel_reason == reason
        assert AuditLog.query.filter_by(action="draw_superseded").one().details[
            "reason"
        ] == reason

    second = client.post(
        f"/admin/events/{event_id}/draw",
        data={"pre_draw_confirmed": "yes"},
        follow_redirects=True,
    )
    assert b"District draw version 2 was finalized" in second.data
    with app.app_context():
        draws = Draw.query.filter_by(event_id=event_id).order_by(Draw.version).all()
        assert [(draw.version, draw.status) for draw in draws] == [
            (1, "superseded"),
            (2, "finalized"),
        ]
        assert AuditLog.query.filter_by(action="draw_finalized").count() == 2


def test_admin_draw_preflight_blocks_unresolved_and_missing_consent(app, client):
    with app.app_context():
        event, church, _ = _seed_event("ready_for_drawing", ["Ready One", "Ready Two"])
        pending = Participant(
            event_id=event.id,
            church_id=church.id,
            full_name="Needs Review",
            name_key="needs review",
            status="under_review",
        )
        admin = User(username="preflight-admin", role="admin")
        admin.set_password("district-password")
        db.session.add_all([pending, admin])
        db.session.commit()
        event_id, pending_id = event.id, pending.id
    _login(client, "preflight-admin", "district-password")
    response = client.post(
        f"/admin/events/{event_id}/draw",
        data={"pre_draw_confirmed": "yes"},
    )
    assert response.status_code == 400
    assert b"All registrations have been reviewed" in response.data
    with app.app_context():
        assert Draw.query.filter_by(event_id=event_id).count() == 0
        pending = db.session.get(Participant, pending_id)
        pending.status = "rejected"
        approved = Participant.query.filter_by(
            event_id=event_id, full_name="Ready One"
        ).one()
        approved.privacy_consent = False
        db.session.commit()
    response = client.get(f"/admin/events/{event_id}/draw")
    assert b"Required participant and guardian consent is recorded" in response.data
    assert b"Action required" in response.data


def test_leader_cannot_access_draw_cancellation_or_credential_reset(app, client):
    with app.app_context():
        event, church, people = _seed_event(
            "drawing_finalized", ["Private Giver", "Private Recipient"]
        )
        draw = Draw(
            event_id=event.id,
            version=1,
            participant_count=2,
            validation_ok=True,
        )
        leader = User(username="draw-outsider", role="leader", church_id=church.id)
        leader.set_password("leader-password")
        db.session.add_all([draw, leader])
        db.session.flush()
        draw_id = draw.id
        db.session.commit()
        event_id, participant_id = event.id, people[0].id
    _login(client, "draw-outsider", "leader-password")
    assert client.get(f"/admin/events/{event_id}/draw").status_code == 403
    assert client.post(
        f"/admin/events/{event_id}/draw/cancel",
        data={"confirm_supersede": "yes", "reason": "Not authorized"},
    ).status_code == 403
    assert client.post(
        f"/admin/participants/{participant_id}/credential/reset",
        data={"reason": "Not authorized"},
    ).status_code == 403
    with app.app_context():
        assert db.session.get(Draw, draw_id).status == "finalized"
        assert AuditLog.query.filter_by(action="draw_superseded").count() == 0


def test_admin_cancel_draw_voids_prepared_packets_and_requires_reason(app, client):
    with app.app_context():
        event, church, people = _seed_event(
            "drawing_finalized", ["Packet Giver", "Packet Recipient"]
        )
        admin = User(username="cancel-admin", role="admin")
        admin.set_password("district-password")
        db.session.add(admin)
        db.session.flush()
        draw = Draw(
            event_id=event.id,
            version=1,
            participant_count=2,
            validation_ok=True,
        )
        db.session.add(draw)
        db.session.flush()
        assignments = [
            Assignment(
                draw_id=draw.id,
                giver_id=person.id,
                recipient_id=people[(index + 1) % 2].id,
            )
            for index, person in enumerate(people)
        ]
        batch = DistributionBatch(
            draw_id=draw.id,
            church_id=church.id,
            created_by_id=admin.id,
        )
        db.session.add_all([*assignments, batch])
        db.session.commit()
        event_id, draw_id, batch_id = event.id, draw.id, batch.id
    _login(client, "cancel-admin", "district-password")
    missing_reason = client.post(
        f"/admin/events/{event_id}/draw/cancel",
        data={"confirm_supersede": "yes", "reason": ""},
        follow_redirects=True,
    )
    assert b"A reason is required" in missing_reason.data
    with app.app_context():
        assert db.session.get(Draw, draw_id).status == "finalized"
    response = client.post(
        f"/admin/events/{event_id}/draw/cancel",
        data={"confirm_supersede": "yes", "reason": "Incorrect packet version"},
        follow_redirects=True,
    )
    assert b"voided" in response.data
    with app.app_context():
        assert db.session.get(Draw, draw_id).status == "superseded"
        assert db.session.get(DistributionBatch, batch_id).status == "void"


def test_admin_withdrawal_after_draw_supersedes_and_creates_new_version(app, client):
    with app.app_context():
        event, church, people = _seed_event(
            "drawing_finalized", ["Withdraw One", "Withdraw Two", "Withdraw Three", "Withdraw Four"]
        )
        admin = User(username="withdraw-draw-admin", role="admin")
        admin.set_password("district-password")
        db.session.add(admin)
        db.session.flush()
        first = Draw(
            event_id=event.id,
            version=1,
            participant_count=4,
            validation_ok=True,
        )
        db.session.add(first)
        db.session.flush()
        assignments = [
            Assignment(
                draw_id=first.id,
                giver_id=person.id,
                recipient_id=people[(index + 1) % 4].id,
            )
            for index, person in enumerate(people)
        ]
        batch = DistributionBatch(
            draw_id=first.id,
            church_id=church.id,
            created_by_id=admin.id,
        )
        db.session.add_all([*assignments, batch])
        db.session.commit()
        event_id, participant_id, first_draw_id = event.id, people[0].id, first.id
    _login(client, "withdraw-draw-admin", "district-password")
    response = client.post(
        f"/admin/events/{event_id}/participants/{participant_id}/withdraw",
        data={"reason": "Participant withdrew after the draw"},
        follow_redirects=True,
    )
    assert b"draw version 2 is now active" in response.data
    with app.app_context():
        event = db.session.get(Event, event_id)
        first = db.session.get(Draw, first_draw_id)
        versions = Draw.query.filter_by(event_id=event_id).order_by(Draw.version).all()
        participant = db.session.get(Participant, participant_id)
        assert event.status == "drawing_finalized"
        assert first.status == "superseded"
        assert first.cancel_reason == "Participant withdrew after the draw"
        assert [draw.version for draw in versions] == [1, 2]
        assert versions[1].status == "finalized"
        assert Assignment.query.filter_by(draw_id=versions[1].id).count() == 3
        assert participant.status == "withdrawn"
        assert participant.retrieval_credential.revoked_at is not None
        assert AuditLog.query.filter_by(
            action="participant_withdrawn_after_draw"
        ).one()
        assert AuditLog.query.filter_by(
            action="draw_finalized"
        ).count() == 1


def test_admin_credential_reset_rotates_pin_clears_lockout_and_audits(app, client):
    with app.app_context():
        event, church, people = _seed_event(
            "distribution", ["Credential Owner", "Credential Recipient"]
        )
        participant = people[0]
        credential = participant.retrieval_credential
        reference = credential.reference
        participant_id = participant.id
        draw = Draw(
            event_id=event.id, version=1, participant_count=2, validation_ok=True
        )
        db.session.add(draw)
        db.session.flush()
        db.session.add_all(
            [
                Assignment(
                    draw_id=draw.id,
                    giver_id=people[0].id,
                    recipient_id=people[1].id,
                ),
                Assignment(
                    draw_id=draw.id,
                    giver_id=people[1].id,
                    recipient_id=people[0].id,
                ),
            ]
        )
        lock_key = rate_limit_key("lookup-reference", reference)
        db.session.add(
            RateLimitAttempt(key=lock_key, window_start=datetime.now(timezone.utc), count=2)
        )
        admin = User(username="credential-admin", role="admin")
        admin.set_password("district-password")
        db.session.add(admin)
        db.session.commit()
        old_hash = credential.pin_hash
    _login(client, "credential-admin", "district-password")
    response = client.post(
        f"/admin/participants/{participant_id}/credential/reset",
        data={"reason": "Participant lost their PIN"},
    )
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert b"Participant PIN reset" in response.data
    assert reference.encode() in response.data
    new_pin = re.search(r"<dd>([2-9]{6})</dd>", text).group(1)
    assert new_pin != "282828"
    assert response.headers["Cache-Control"] == "no-store"
    with app.app_context():
        credential = RetrievalCredential.query.filter_by(reference=reference).one()
        assert credential.pin_hash != old_hash
        assert verify_pin(credential.pin_hash, new_pin)
        assert not verify_pin(credential.pin_hash, "282828")
        assert RateLimitAttempt.query.filter_by(key=lock_key).count() == 0
        reset = AuditLog.query.filter_by(
            action="participant_credential_reset"
        ).one()
        assert reset.entity_id == str(participant_id)
        assert reset.details["reason"] == "Participant lost their PIN"
        assert new_pin not in str(reset.details)

    old_pin = client.post(
        "/lookup", data={"reference": reference, "pin": "282828"}
    )
    assert b"Credential Recipient" not in old_pin.data
    new_pin_attempt = client.post(
        "/lookup", data={"reference": reference, "pin": new_pin}
    )
    assert b"Credential Recipient" in new_pin_attempt.data


def test_admin_reset_rejects_missing_reason_and_non_admins(app, client):
    with app.app_context():
        event, church, people = _seed_event("registration_open", ["Protected Youth"])
        participant_id = people[0].id
        leader = User(username="reset-outsider", role="leader", church_id=church.id)
        leader.set_password("leader-password")
        admin = User(username="reset-admin", role="admin")
        admin.set_password("district-password")
        db.session.add_all([leader, admin])
        db.session.commit()
    _login(client, "reset-outsider", "leader-password")
    denied = client.post(
        f"/admin/participants/{participant_id}/credential/reset",
        data={"reason": "Should not run"},
    )
    assert denied.status_code == 403
    with app.app_context():
        credential = Participant.query.filter_by(full_name="Protected Youth").one().retrieval_credential
        original_hash = credential.pin_hash
    _login(client, "reset-admin", "district-password")
    missing_reason = client.post(
        f"/admin/participants/{participant_id}/credential/reset",
        data={"reason": ""},
        follow_redirects=True,
    )
    assert b"A reason is required to reset participant credentials" in missing_reason.data
    with app.app_context():
        credential = Participant.query.filter_by(full_name="Protected Youth").one().retrieval_credential
        assert credential.pin_hash == original_hash
        assert AuditLog.query.filter_by(
            action="participant_credential_reset_rejected"
        ).one()


def test_admin_event_lifecycle_transition_is_audited(app, client):
    with app.app_context():
        event, _, _ = _seed_event("draft")
        admin = User(username="admin", role="admin")
        admin.set_password("district-password")
        db.session.add(admin)
        db.session.commit()
        event_id = event.id
    _login(client, "admin", "district-password")
    response = client.post(
        f"/admin/events/{event_id}/transition",
        data={"status": "registration_open"},
    )
    assert response.status_code == 302
    with app.app_context():
        event = db.session.get(Event, event_id)
        log = AuditLog.query.filter_by(action="event_status_changed").one()
        assert event.status == "registration_open"
        assert log.details == {"from": "draft", "to": "registration_open"}


def test_withdrawal_supersedes_whole_draw_and_redraws(app):
    with app.app_context():
        event, church, people = _seed_event(
            "drawing_finalized", ["A", "B", "C", "D"]
        )
        first = Draw(
            event_id=event.id,
            version=1,
            participant_count=len(people),
            validation_ok=True,
        )
        db.session.add(first)
        db.session.flush()
        assignments = [
            Assignment(draw_id=first.id, giver_id=person.id, recipient_id=people[(i + 1) % 4].id)
            for i, person in enumerate(people)
        ]
        db.session.add_all(assignments)
        db.session.commit()
        new_draw = withdraw_after_draw(event, people[0], "Participant moved away")
        db.session.refresh(first)
        db.session.refresh(event)
        db.session.refresh(people[0])
        assert first.status == "superseded"
        assert first.cancel_reason == "Participant moved away"
        assert people[0].status == "withdrawn"
        assert people[0].withdrawal_reason == "Participant moved away"
        assert RegistrationStatusHistory.query.filter_by(
            participant_id=people[0].id, new_status="withdrawn"
        ).one().reason == "Participant moved away"
        assert new_draw.version == 2
        assert event.status == "drawing_finalized"
        assert Assignment.query.filter_by(draw_id=new_draw.id).count() == 3


def test_assignment_access_is_logged_for_the_participant_lookup(app, client):
    with app.app_context():
        event, church, people = _seed_event("distribution", ["Giver", "Recipient"])
        draw = Draw(event_id=event.id, version=1, participant_count=2, validation_ok=True)
        db.session.add(draw)
        db.session.flush()
        assignment = Assignment(
            draw_id=draw.id,
            giver_id=people[0].id,
            recipient_id=people[1].id,
        )
        db.session.add_all(
            [
                assignment,
                Assignment(
                    draw_id=draw.id,
                    giver_id=people[1].id,
                    recipient_id=people[0].id,
                ),
            ]
        )
        reference = people[0].retrieval_credential.reference
        assignment_id, participant_id = assignment.id, people[0].id
        db.session.commit()
    response = client.post("/lookup", data={"reference": reference, "pin": "282828"})
    assert response.status_code == 200
    with app.app_context():
        access_log = AssignmentAccessLog.query.one()
        assert access_log.assignment_id == assignment_id
        assert access_log.participant_id == participant_id
        assert access_log.access_method == "participant_lookup"


def test_withdrawal_preserves_draw_when_too_few_people_would_remain(app):
    with app.app_context():
        event, church, people = _seed_event("drawing_finalized", ["A", "B"])
        first = Draw(
            event_id=event.id,
            version=1,
            participant_count=2,
            validation_ok=True,
        )
        db.session.add(first)
        db.session.flush()
        db.session.add_all(
            [
                Assignment(draw_id=first.id, giver_id=people[0].id, recipient_id=people[1].id),
                Assignment(draw_id=first.id, giver_id=people[1].id, recipient_id=people[0].id),
            ]
        )
        db.session.commit()
        with pytest.raises(ValueError, match="At least two approved participants"):
            withdraw_after_draw(event, people[0], "Cannot continue")
        db.session.refresh(event)
        db.session.refresh(first)
        db.session.refresh(people[0])
        assert event.status == "drawing_finalized"
        assert first.status == "finalized"
        assert people[0].status == "approved"


def test_church_scope_policy_allows_admin_and_only_the_leaders_church(app):
    with app.app_context():
        own_church = Church(name="Scope Church")
        other_church = Church(name="Other Scope Church")
        db.session.add_all([own_church, other_church])
        db.session.flush()
        leader = User(username="scoped-leader", role="leader", church_id=own_church.id)
        admin = User(username="scope-admin", role="admin")
        leader.set_password("leader-password")
        admin.set_password("admin-password")
        db.session.add_all([leader, admin])
        db.session.flush()
        assert can_access_church(own_church.id, leader)
        assert not can_access_church(other_church.id, leader)
        assert can_access_church(other_church.id, admin)
        assert not can_access_church(other_church.id, None)


def test_user_role_requires_the_correct_church_scope(app):
    from sqlalchemy.exc import IntegrityError

    with app.app_context():
        church = Church(name="Constraint Church")
        db.session.add(church)
        db.session.flush()
        invalid_admin = User(username="scoped-admin", role="admin", church_id=church.id)
        invalid_admin.set_password("admin-password")
        db.session.add(invalid_admin)
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()


def test_admin_and_leader_decorators_enforce_roles(app, client):
    with app.app_context():
        church = Church(name="Role Church")
        db.session.add(church)
        db.session.flush()
        leader = User(username="role-leader", role="leader", church_id=church.id)
        leader.set_password("leader-password")
        admin = User(username="role-admin", role="admin")
        admin.set_password("admin-password")
        db.session.add_all([leader, admin])
        db.session.commit()
    assert client.get("/admin/").status_code == 302
    _login(client, "role-leader", "leader-password")
    assert client.get("/leader/").status_code == 200
    assert client.get("/admin/").status_code == 403
    client.post("/logout")
    _login(client, "role-admin", "admin-password")
    assert client.get("/admin/").status_code == 200
    assert client.get("/leader/").status_code == 403


def test_login_session_contains_only_user_id_and_logout_clears_it(app, client):
    with app.app_context():
        admin = User(username="session-admin", role="admin")
        admin.set_password("session-admin-password")
        db.session.add(admin)
        db.session.commit()
        admin_id = str(admin.id)

    _login(client, "session-admin", "session-admin-password")
    with client.session_transaction() as signed_session:
        assert signed_session["user_id"] == admin_id
        assert "role" not in signed_session
        assert "password" not in signed_session

    assert client.post("/logout").status_code == 302
    with client.session_transaction() as signed_session:
        assert "user_id" not in signed_session


def test_session_role_claims_cannot_grant_access(app, client):
    with client.session_transaction() as signed_session:
        signed_session["user_id"] = str(uuid.uuid4())
        signed_session["role"] = "admin"
    assert client.get("/admin/").status_code == 302
    with client.session_transaction() as signed_session:
        assert "user_id" not in signed_session


def test_create_admin_prompts_for_password_and_stores_scrypt(app):
    runner = CliRunner()
    result = runner.invoke(
        app.cli,
        ["create-admin", "--username", "first-admin"],
        input="A-Strong-Admin-Password-1!\nA-Strong-Admin-Password-1!\n",
    )
    assert result.exit_code == 0, result.output
    assert "Password:" in result.output
    with app.app_context():
        admin = User.query.filter_by(username="first-admin").one()
        assert admin.role == "admin"
        assert admin.church_id is None
        assert admin.password_hash.startswith("scrypt:")
        assert admin.check_password("A-Strong-Admin-Password-1!")
        assert AuditLog.query.filter_by(action="user_created").count() == 1


def test_distribution_batch_and_record_constraints(app):
    from sqlalchemy.exc import IntegrityError

    with app.app_context():
        event, church, people = _seed_event("distribution", ["A", "B"])
        draw = Draw(event_id=event.id, version=1, participant_count=2, validation_ok=True)
        admin = User(username="distribution-admin", role="admin")
        admin.set_password("distribution-password")
        db.session.add_all([draw, admin])
        db.session.flush()
        assignment = Assignment(
            draw_id=draw.id, giver_id=people[0].id, recipient_id=people[1].id
        )
        db.session.add(assignment)
        db.session.flush()
        batch = DistributionBatch(
            draw_id=draw.id,
            church_id=church.id,
            created_by_id=admin.id,
            status="prepared",
        )
        db.session.add(batch)
        db.session.flush()
        record = DistributionRecord(
            batch_id=batch.id,
            assignment_id=assignment.id,
            status="pending",
        )
        db.session.add(record)
        db.session.commit()

        invalid_record = DistributionRecord(
            batch_id=batch.id,
            assignment_id=assignment.id,
            status="delivered",
        )
        db.session.add(invalid_record)
        with pytest.raises(IntegrityError):
            db.session.commit()


def _seed_draw_packet(event_status="distribution", names=("Packet Giver", "Packet Recipient")):
    event, church, people = _seed_event(event_status, names)
    draw = Draw(
        event_id=event.id,
        version=1,
        participant_count=len(people),
        validation_ok=True,
    )
    admin = User(username=f"packet-admin-{uuid.uuid4().hex[:8]}", role="admin")
    admin.set_password("packet-admin-password")
    leader = User(
        username=f"packet-leader-{uuid.uuid4().hex[:8]}",
        role="leader",
        church_id=church.id,
    )
    leader.set_password("packet-leader-password")
    db.session.add_all([draw, admin, leader])
    db.session.flush()
    db.session.add_all(
        Assignment(
            draw_id=draw.id,
            giver_id=person.id,
            recipient_id=people[(index + 1) % len(people)].id,
        )
        for index, person in enumerate(people)
    )
    db.session.commit()
    return event, church, people, draw, admin, leader


def test_admin_generates_private_envelope_packet_and_tracks_acknowledgment(
    app, client, tmp_path
):
    from pathlib import Path

    app.config["PRIVATE_STORAGE_DIR"] = str(tmp_path / "private-packets")
    with app.app_context():
        event, church, people, draw, admin, leader = _seed_draw_packet()
        event_id, church_id = event.id, church.id
        draw_id = draw.id
        admin_name, leader_name = admin.username, leader.username
        initial_credentials = {
            person.id: (person.retrieval_credential.reference, person.retrieval_credential.pin_hash)
            for person in people
        }
    _login(client, admin_name, "packet-admin-password")
    control = client.get(f"/admin/events/{event_id}/distribution")
    assert control.status_code == 200
    assert b"North Church" in control.data
    generated = client.post(
        f"/admin/events/{event_id}/distribution/churches/{church_id}/generate"
    )
    assert generated.status_code == 302

    with app.app_context():
        batch = DistributionBatch.query.filter_by(draw_id=draw_id, church_id=church_id).one()
        batch_id = batch.id
        records = DistributionRecord.query.filter_by(batch_id=batch.id).all()
        assert len(records) == 2
        record_ids = [record.id for record in records]
        packet_path = Path(app.config["PRIVATE_STORAGE_DIR"]) / batch.packet_path
        assert batch.packet_generated_at is not None
        assert batch.version == 1
        rotated_hashes = []
        for person in people:
            credential = RetrievalCredential.query.filter_by(participant_id=person.id).one()
            old_reference, old_hash = initial_credentials[person.id]
            assert credential.reference == old_reference
            assert credential.pin_hash != old_hash
            assert not verify_pin(credential.pin_hash, "282828")
            rotated_hashes.append(credential.pin_hash)
    packet_html = packet_path.read_text(encoding="utf-8")
    assert packet_path.suffix == ".html"
    assert "<!doctype html>" in packet_html.lower()
    assert "OUTSIDE LABEL" in packet_html
    assert "CONFIDENTIAL INSERT" in packet_html
    assert "Private reference" in packet_html
    assert any(reference in packet_html for reference, _ in initial_credentials.values())
    packet_pins = re.findall(r'<dd class="pin">([2-9]{6})</dd>', packet_html)
    assert len(packet_pins) == 2
    assert all(
        any(verify_pin(pin_hash, pin) for pin in packet_pins)
        for pin_hash in rotated_hashes
    )
    assert packet_html.count('class="assignment-card"') == 2
    assert "break-after: page" in packet_html
    assert "Print / Save as PDF" in packet_html
    assert Path(app.static_folder).resolve() not in packet_path.resolve().parents

    packet_response = client.get(f"/admin/distribution/batches/{batch_id}/pdf")
    assert packet_response.status_code == 200
    assert packet_response.mimetype == "text/html"
    assert "no-store" in packet_response.headers["Cache-Control"]
    assert b"Print / Save as PDF" in packet_response.data
    card_response = client.get(
        f"/admin/distribution/batches/{batch_id}/records/{record_ids[0]}/pdf"
    )
    assert card_response.status_code == 200
    assert card_response.mimetype == "text/html"
    assert card_response.data.count(b'class="assignment-card"') == 1

    _login(client, leader_name, "packet-leader-password")
    assert client.get(f"/admin/events/{event_id}/distribution").status_code == 403
    assert (
        client.post(
            f"/admin/events/{event_id}/distribution/churches/{church_id}/generate"
        ).status_code
        == 403
    )
    assert client.get(f"/admin/distribution/batches/{batch_id}/pdf").status_code == 403
    assert (
        client.get(
            f"/admin/distribution/batches/{batch_id}/records/{record_ids[0]}/pdf"
        ).status_code
        == 403
    )
    _login(client, admin_name, "packet-admin-password")
    assert client.post(f"/admin/distribution/batches/{batch_id}/printed").status_code == 302
    assert client.post(f"/admin/distribution/batches/{batch_id}/distributed").status_code == 302
    first_ack = client.post(
        f"/admin/distribution/batches/{batch_id}/records/{record_ids[0]}/acknowledge"
    )
    assert first_ack.status_code == 302
    with app.app_context():
        assert db.session.get(DistributionBatch, batch_id).acknowledged_at is None
    second_ack = client.post(
        f"/admin/distribution/batches/{batch_id}/records/{record_ids[1]}/acknowledge"
    )
    assert second_ack.status_code == 302
    with app.app_context():
        batch = db.session.get(DistributionBatch, batch_id)
        assert batch.status == "distributed"
        assert batch.print_count == 1
        assert batch.printed_at is not None
        assert batch.distributed_at is not None
        assert batch.acknowledged_at is not None
        assert DistributionRecord.query.filter_by(
            batch_id=batch_id, status="delivered"
        ).count() == 2
        assert AuditLog.query.filter_by(action="distribution_packet_generated").count() == 1
        generation_audit = AuditLog.query.filter_by(
            action="distribution_packet_generated"
        ).one()
        assert all(pin not in str(generation_audit.details) for pin in packet_pins)
        assert AuditLog.query.filter_by(action="distribution_packet_downloaded").count() == 1
        assert AuditLog.query.filter_by(action="distribution_card_downloaded").count() == 1
        assert AuditLog.query.filter_by(action="distribution_packet_printed").count() == 1
        assert AuditLog.query.filter_by(action="distribution_packet_distributed").count() == 1
        assert AuditLog.query.filter_by(
            action="distribution_assignment_acknowledged"
        ).count() == 2
    status_report = client.get(
        f"/admin/reports/distribution-status.csv?event_id={event_id}"
    )
    assert status_report.status_code == 200
    assert b"North Church,1,distributed" in status_report.data
    assert b",2,2,0," in status_report.data


def test_packet_replacement_rotates_pins_and_invalidates_old_download(app, client, tmp_path):
    app.config["PRIVATE_STORAGE_DIR"] = str(tmp_path / "private-packets")
    with app.app_context():
        event, church, people, draw, admin, _ = _seed_draw_packet()
        event_id, church_id, admin_name = event.id, church.id, admin.username
        draw_id = draw.id
        original_hashes = {
            person.id: person.retrieval_credential.pin_hash for person in people
        }
    _login(client, admin_name, "packet-admin-password")
    client.post(f"/admin/events/{event_id}/distribution/churches/{church_id}/generate")
    with app.app_context():
        old_batch = DistributionBatch.query.filter_by(draw_id=draw_id).one()
        old_batch_id = old_batch.id
    replacement_reason = "A confidential insert was damaged before handoff"
    replaced = client.post(
        f"/admin/events/{event_id}/distribution/churches/{church_id}/generate",
        data={"reason": replacement_reason},
    )
    assert replaced.status_code == 302
    assert client.get(f"/admin/distribution/batches/{old_batch_id}/pdf").status_code == 404
    with app.app_context():
        batches = DistributionBatch.query.filter_by(draw_id=draw_id).order_by(
            DistributionBatch.version
        ).all()
        old_batch, new_batch = batches
        assert old_batch.status == "void"
        assert new_batch.version == 2
        assert new_batch.replacement_of_id == old_batch.id
        assert new_batch.replacement_reason == replacement_reason
        for person in people:
            credential = RetrievalCredential.query.filter_by(participant_id=person.id).one()
            assert credential.pin_hash != original_hashes[person.id]
            assert not verify_pin(credential.pin_hash, "282828")
        replacement_audit = AuditLog.query.filter_by(
            action="distribution_packet_replaced"
        ).one()
        assert replacement_audit.details["reason"] == replacement_reason


def test_packet_generation_rejects_static_storage_without_rotating_pins(app, client):
    app.config["PRIVATE_STORAGE_DIR"] = app.static_folder
    with app.app_context():
        event, church, people, draw, admin, _ = _seed_draw_packet()
        event_id, church_id, draw_id = event.id, church.id, draw.id
        admin_name = admin.username
        initial_hashes = {
            person.id: person.retrieval_credential.pin_hash for person in people
        }
    _login(client, admin_name, "packet-admin-password")
    response = client.post(
        f"/admin/events/{event_id}/distribution/churches/{church_id}/generate",
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"outside the static directory" in response.data
    with app.app_context():
        assert DistributionBatch.query.filter_by(draw_id=draw_id).count() == 0
        for person in people:
            credential = RetrievalCredential.query.filter_by(participant_id=person.id).one()
            assert credential.pin_hash == initial_hashes[person.id]
        assert AuditLog.query.filter_by(
            action="distribution_packet_generation_rejected"
        ).one()


def test_superseding_draw_invalidates_generated_private_packets(app, client, tmp_path):
    from app.services.distribution import generate_church_packet

    app.config["PRIVATE_STORAGE_DIR"] = str(tmp_path / "private-packets")
    with app.app_context():
        event, church, people, draw, admin, _ = _seed_draw_packet(
            event_status="drawing_finalized"
        )
        admin_id = admin.id
        admin_name = admin.username
        batch = generate_church_packet(event.id, church.id, admin_id)
        batch_id, event_id = batch.id, event.id
        cancel_draw(event, "Approved participant roster changed")
        assert db.session.get(DistributionBatch, batch_id).status == "void"
    _login(client, admin_name, "packet-admin-password")
    assert client.get(f"/admin/distribution/batches/{batch_id}/pdf").status_code == 404


def test_admin_reports_and_assignment_exports_are_authorized_and_audited(app, client):
    with app.app_context():
        event, church, people, draw, admin, leader = _seed_draw_packet()
        withdrawn = Participant(
            event_id=event.id,
            church_id=church.id,
            full_name="DEMO Withdrawn Report",
            name_key="demo withdrawn report",
            status="withdrawn",
        )
        formula_name = Participant(
            event_id=event.id,
            church_id=church.id,
            full_name='=HYPERLINK("https://example.invalid")',
            name_key="formula test",
            status="rejected",
        )
        db.session.add_all([withdrawn, formula_name])
        db.session.flush()
        db.session.add(
            RegistrationStatusHistory(
                participant_id=withdrawn.id,
                previous_status="approved",
                new_status="withdrawn",
                reason="Synthetic report test withdrawal",
            )
        )
        event_id, admin_name, leader_name = event.id, admin.username, leader.username
        db.session.commit()
    _login(client, admin_name, "packet-admin-password")
    page = client.get(f"/admin/reports?event_id={event_id}")
    assert page.status_code == 200
    for label in (
        b"Masterlist",
        b"Church summary",
        b"Drawing validation",
        b"Distribution status",
        b"Withdrawals",
        b"Confidential assignment export CSV",
    ):
        assert label in page.data

    masterlist = client.get(
        f"/admin/reports/masterlist.csv?event_id={event_id}"
    )
    assert masterlist.status_code == 200
    assert masterlist.data.startswith(b"\xef\xbb\xbf")
    assert b"Packet Giver" in masterlist.data
    assert b"'=HYPERLINK" in masterlist.data
    assert b"pin_hash" not in masterlist.data

    church_summary = client.get(
        f"/admin/reports/church-summary.csv?event_id={event_id}"
    )
    assert b"North Church,4,2,0,1,1" in church_summary.data
    validation = client.get(
        f"/admin/reports/drawing-validation.csv?event_id={event_id}"
    )
    assert b"Current assignment derangement,All approved participants have a valid assignment." in validation.data
    withdrawals = client.get(
        f"/admin/reports/withdrawals.csv?event_id={event_id}"
    )
    assert b"DEMO Withdrawn Report" in withdrawals.data
    distribution = client.get(
        f"/admin/reports/distribution-status.csv?event_id={event_id}"
    )
    assert distribution.status_code == 200
    audit_export = client.get("/admin/reports/audit.csv")
    assert audit_export.status_code == 200
    assignment_export = client.get(
        f"/admin/reports/assignments.csv?event_id={event_id}"
    )
    assert assignment_export.status_code == 200
    assert assignment_export.data.startswith(b"\xef\xbb\xbf")
    assert b"Packet Giver" in assignment_export.data
    assert b"Packet Recipient" in assignment_export.data
    assert b"CONFIDENTIAL-assignments" in assignment_export.headers["Content-Disposition"].encode()

    _login(client, leader_name, "packet-leader-password")
    assert client.get(f"/admin/reports?event_id={event_id}").status_code == 403
    assert (
        client.get(
            f"/admin/reports/assignments.csv?event_id={event_id}"
        ).status_code
        == 403
    )
    with app.app_context():
        assert AuditLog.query.filter_by(action="assignment_export_downloaded").count() == 1
        assert AuditLog.query.filter_by(action="report_export_downloaded").count() == 6


def test_seed_demo_creates_labeled_synthetic_data_only_once(app):
    runner = CliRunner()
    unconfirmed = runner.invoke(app.cli, ["seed-demo"])
    assert unconfirmed.exit_code != 0
    assert "--confirm-demo" in unconfirmed.output
    with app.app_context():
        assert Event.query.filter(Event.name.like("DEMO%")).count() == 0

    seeded = runner.invoke(app.cli, ["seed-demo", "--confirm-demo"])
    assert seeded.exit_code == 0, seeded.output
    assert "DEMO - District Youth Bunotan Sample" in seeded.output
    assert "No user accounts were created" in seeded.output
    assert not re.search(r"\b[2-9]{6}\b", seeded.output)
    with app.app_context():
        event = Event.query.filter_by(name="DEMO - District Youth Bunotan Sample").one()
        assert event.status == "distribution"
        assert Church.query.filter(Church.name.like("DEMO%")).count() == 3
        participants = Participant.query.filter_by(event_id=event.id).all()
        assert len(participants) == 5
        assert all(person.full_name.startswith("DEMO ") for person in participants)
        assert Draw.query.filter_by(event_id=event.id, status="finalized").one()
        assert Assignment.query.join(Draw).filter(Draw.event_id == event.id).count() == 4
        assert User.query.count() == 0
        seeded_audit = AuditLog.query.filter_by(action="demo_data_seeded").one()
        assert seeded_audit.details["synthetic_data_only"] is True
    repeated = runner.invoke(app.cli, ["seed-demo", "--confirm-demo"])
    assert repeated.exit_code == 0
    assert "already exists" in repeated.output
    with app.app_context():
        assert Event.query.filter_by(name="DEMO - District Youth Bunotan Sample").count() == 1


def test_clear_demo_removes_only_seeded_data_and_private_packets(app, tmp_path):
    runner = CliRunner()
    app.config["PRIVATE_STORAGE_DIR"] = str(tmp_path / "private")
    seeded = runner.invoke(app.cli, ["seed-demo", "--confirm-demo"])
    assert seeded.exit_code == 0, seeded.output

    with app.app_context():
        event = Event.query.filter_by(
            name="DEMO - District Youth Bunotan Sample"
        ).one()
        north_church = Church.query.filter_by(name="DEMO - North Church").one()
        draw = Draw.query.filter_by(event_id=event.id).one()
        assignment = Assignment.query.filter_by(draw_id=draw.id).first()
        admin = User(username="clear-demo-admin", role="admin")
        leader = User(
            username="clear-demo-leader",
            role="leader",
            church_id=north_church.id,
        )
        admin.set_password("clear-demo-admin-password")
        leader.set_password("clear-demo-leader-password")
        other_event = Event(name="Unrelated Local Event", status="registration_open")
        db.session.add_all([admin, leader, other_event])
        db.session.flush()
        unrelated = Participant(
            event_id=other_event.id,
            church_id=north_church.id,
            full_name="Unrelated Local Youth",
            name_key="unrelated local youth",
        )
        batch = DistributionBatch(
            draw_id=draw.id,
            church_id=north_church.id,
            created_by_id=admin.id,
            packet_path="pending",
            packet_generated_at=datetime.now(timezone.utc),
        )
        db.session.add(batch)
        db.session.flush()
        batch_id = batch.id
        record = DistributionRecord(
            batch_id=batch.id,
            assignment_id=assignment.id,
            card_path="pending",
        )
        db.session.add_all([unrelated, record])
        db.session.commit()
        event_id = event.id
        other_event_id = other_event.id
        north_church_id = north_church.id

    packet_directory = tmp_path / "private" / "distribution" / str(batch_id)
    packet_directory.mkdir(parents=True)
    (packet_directory / "church-packet.html").write_text(
        "DEMO packet", encoding="utf-8"
    )

    unconfirmed = runner.invoke(app.cli, ["clear-demo"])
    assert unconfirmed.exit_code != 0
    assert "--confirm-clear" in unconfirmed.output
    with app.app_context():
        assert db.session.get(Event, event_id) is not None

    cleared = runner.invoke(app.cli, ["clear-demo", "--confirm-clear"])
    assert cleared.exit_code == 0, cleared.output
    assert "Cleared seeded DEMO data" in cleared.output
    assert not packet_directory.exists()
    with app.app_context():
        assert db.session.get(Event, event_id) is None
        assert db.session.get(Event, other_event_id) is not None
        assert Participant.query.filter_by(event_id=event_id).count() == 0
        assert Participant.query.filter_by(full_name="Unrelated Local Youth").count() == 1
        assert db.session.get(Church, north_church_id) is not None
        assert Church.query.filter_by(name="DEMO - Central Church").count() == 0
        assert Church.query.filter_by(name="DEMO - South Church").count() == 0
        assert User.query.filter_by(username="clear-demo-admin").count() == 1
        assert User.query.filter_by(username="clear-demo-leader").count() == 1
        assert Draw.query.filter_by(event_id=event_id).count() == 0
        assert DistributionBatch.query.filter_by(id=batch_id).count() == 0
        assert AuditLog.query.filter_by(action="demo_data_seeded").count() == 0

    repeated = runner.invoke(app.cli, ["clear-demo", "--confirm-clear"])
    assert repeated.exit_code == 0
    assert "No seeded DEMO event was found" in repeated.output


def test_demo_admin_bootstrap_is_opt_in_and_idempotent(app, monkeypatch):
    runner = CliRunner()
    disabled = runner.invoke(app.cli, ["bootstrap-demo-admin"])
    assert disabled.exit_code != 0
    with app.app_context():
        assert User.query.count() == 0

    monkeypatch.setenv("BUNOTAN_DEMO_ADMIN_BOOTSTRAP", "1")
    monkeypatch.setenv("BUNOTAN_DEMO_ADMIN_USERNAME", "render-demo-admin")
    monkeypatch.setenv("BUNOTAN_DEMO_ADMIN_PASSWORD", "long-demo-password-123")
    created = runner.invoke(app.cli, ["bootstrap-demo-admin"])
    assert created.exit_code == 0, created.output
    assert "Disposable demo admin created" in created.output
    assert "long-demo-password-123" not in created.output
    with app.app_context():
        user = User.query.filter_by(username="render-demo-admin").one()
        assert user.role == "admin"
        assert user.active
        assert user.check_password("long-demo-password-123")
        original_hash = user.password_hash

    repeated = runner.invoke(app.cli, ["bootstrap-demo-admin"])
    assert repeated.exit_code == 0, repeated.output
    assert "already exists" in repeated.output
    with app.app_context():
        assert User.query.filter_by(username="render-demo-admin").count() == 1
        assert User.query.filter_by(username="render-demo-admin").one().password_hash == original_hash
        assert AuditLog.query.filter_by(action="user_created").count() == 1


def test_argon2_hashes_cannot_be_verified_with_the_new_hasher(app):
    with app.app_context():
        legacy = User(
            username="legacy-admin",
            role="admin",
            password_hash="$argon2id$legacy-hash-unverifiable",
        )
        db.session.add(legacy)
        db.session.commit()
        assert not legacy.check_password("legacy-password")


def test_reset_all_credentials_rehashes_staff_rotates_pins_and_voids_packets(
    app, tmp_path
):
    private_dir = tmp_path / "private"
    app.config["PRIVATE_STORAGE_DIR"] = str(private_dir)
    with app.app_context():
        event, church, people = _seed_event("drawing_finalized", ["A", "B"])
        admin = User(
            username="reset-admin",
            role="admin",
            password_hash="$argon2id$legacy-admin-hash",
        )
        db.session.add(admin)
        db.session.flush()
        draw = Draw(
            event_id=event.id,
            version=1,
            participant_count=2,
            validation_ok=True,
        )
        db.session.add(draw)
        db.session.flush()
        assignments = [
            Assignment(
                draw_id=draw.id,
                giver_id=person.id,
                recipient_id=people[1 - index].id,
            )
            for index, person in enumerate(people)
        ]
        db.session.add_all(assignments)
        db.session.flush()
        batch = DistributionBatch(
            draw_id=draw.id,
            church_id=church.id,
            created_by_id=admin.id,
            packet_path="distribution/old/church-packet.pdf",
            packet_generated_at=datetime.now(timezone.utc),
        )
        db.session.add(batch)
        db.session.commit()
        first_participant_id = people[0].id
        old_pin_hash = people[0].retrieval_credential.pin_hash
        old_reference = people[0].retrieval_credential.reference

    result = CliRunner().invoke(
        app.cli,
        ["reset-all-credentials", "--confirm-reset"],
        input="New-Admin-Password-123!\nNew-Admin-Password-123!\n",
    )
    assert result.exit_code == 0, result.output
    exports = list((private_dir / "credential-reset").glob("*.csv"))
    assert len(exports) == 1
    with exports[0].open(encoding="utf-8-sig", newline="") as source:
        rows = list(csv.DictReader(source))
    assert len(rows) == 2
    assert {"event", "church", "participant", "reference", "pin"} == set(rows[0])
    first_credentials = next(row for row in rows if row["participant"] == "A")

    with app.app_context():
        admin = User.query.filter_by(username="reset-admin").one()
        assert admin.check_password("New-Admin-Password-123!")
        assert admin.password_hash.startswith("scrypt:")
        participant = db.session.get(Participant, first_participant_id)
        assert participant.retrieval_credential.reference == first_credentials["reference"]
        assert participant.retrieval_credential.reference != old_reference
        assert participant.retrieval_credential.pin_hash != old_pin_hash
        assert verify_pin(
            participant.retrieval_credential.pin_hash, first_credentials["pin"]
        )
        batch = DistributionBatch.query.one()
        assert batch.status == "void"
        assert batch.packet_path is None
        assert batch.packet_generated_at is None
        assert AuditLog.query.filter_by(action="all_credentials_reset").count() == 1
        assert AuditLog.query.filter_by(action="distribution_packet_invalidated").count() == 1
