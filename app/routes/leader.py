import csv
import io
import uuid

from flask import (
    Blueprint,
    abort,
    flash,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)

from ..auth_state import current_user
from ..models import Church, Event, Participant, db
from ..services.access import leader_required, require_church_scope
from ..services.audit import record_audit
from ..services.participants import (
    IMPORT_HEADERS,
    ParticipantError,
    add_leader_batch,
    add_participant,
    parse_import_file,
    preview_leader_import,
)
from ..services.rate_limits import rate_limit

bp = Blueprint("leader", __name__)


@bp.get("/")
@leader_required
def dashboard():
    require_church_scope(current_user.church_id)
    return _render_dashboard()


@bp.post("/submit")
@leader_required
def submit():
    require_church_scope(current_user.church_id)
    try:
        event, church = _get_import_context()
        participant, pin = add_participant(
            event,
            church,
            request.form.get("full_name", ""),
            request.form.get("guardian_consent") == "yes",
            is_minor=request.form.get("is_minor") == "yes",
            privacy_consent=request.form.get("privacy_consent") == "yes",
            participation_confirmed=(
                request.form.get("participation_confirmed") == "yes"
            ),
            source="church_leader",
        )
        db.session.commit()
        flash("Participant submitted. Share these credentials with the participant now.", "success")
        return _render_dashboard(
            credentials={
                "reference": participant.retrieval_credential.reference,
                "pin": pin,
            }
        )
    except ParticipantError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    except Exception:
        db.session.rollback()
        raise
    return redirect(url_for("leader.dashboard"))


@bp.post("/import/preview")
@leader_required
@rate_limit("leader-import-preview", 10, 3600, 3600)
def preview_import():
    require_church_scope(current_user.church_id)
    try:
        event, church = _get_import_context()
        _require_import_consents()
        rows = preview_leader_import(event, church, _uploaded_rows())
        record_audit(
            "leader_import_previewed",
            "event",
            event.id,
            {
                "church_id": str(church.id),
                "row_count": len(rows),
                "eligible_count": sum(row["eligible"] for row in rows),
            },
        )
        db.session.commit()
        if _is_ajax():
            return jsonify(_preview_response(rows))
        return render_template(
            "leader/import_preview.html",
            event=event,
            rows=rows,
            eligible_count=sum(row["eligible"] for row in rows),
        )
    except ParticipantError as exc:
        db.session.rollback()
        if _is_ajax():
            return jsonify(error=str(exc)), 400
        flash(str(exc), "danger")
    except Exception:
        db.session.rollback()
        raise
    return redirect(url_for("leader.dashboard"))


@bp.post("/import/confirm")
@leader_required
@rate_limit("leader-import-confirm", 10, 3600, 3600)
def confirm_import():
    require_church_scope(current_user.church_id)
    try:
        event, church = _get_import_context()
        _require_import_consents()
        rows = preview_leader_import(event, church, _uploaded_rows())
        created, rows = add_leader_batch(event, church, rows)
        record_audit(
            "leader_import_completed",
            "event",
            event.id,
            {
                "church_id": str(church.id),
                "submitted_count": len(created),
                "skipped_count": len(rows) - len(created),
            },
        )
        db.session.commit()
        credentials = [
            {
                "name": participant.full_name,
                "reference": participant.retrieval_credential.reference,
                "pin": pin,
            }
            for participant, pin in created
        ]
        if _is_ajax():
            return jsonify(
                {
                    **_preview_response(rows),
                    "message": f"{len(created)} eligible participant(s) submitted as pending.",
                    "credentials": credentials,
                }
            )
        return render_template(
            "leader/import_preview.html",
            event=event,
            rows=rows,
            eligible_count=0,
            credentials_list=credentials,
            completed=True,
        )
    except ParticipantError as exc:
        db.session.rollback()
        if _is_ajax():
            return jsonify(error=str(exc)), 400
        flash(str(exc), "danger")
    except Exception:
        db.session.rollback()
        raise
    return redirect(url_for("leader.dashboard"))


@bp.post("/import/error-report")
@leader_required
@rate_limit("leader-import-error-report", 10, 3600, 3600)
def import_error_report():
    require_church_scope(current_user.church_id)
    try:
        event, church = _get_import_context()
        _require_import_consents()
        rows = preview_leader_import(event, church, _uploaded_rows())
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(("row_number", "full_name", "issue_type", "details"))
        issue_count = 0
        for row in rows:
            for issue_type, messages in (
                ("error", row["errors"]),
                ("duplicate_warning", row["warnings"]),
            ):
                for message in messages:
                    writer.writerow(
                        (
                            row["row_number"],
                            _safe_csv_value(row["full_name"]),
                            issue_type,
                            message,
                        )
                    )
                    issue_count += 1
        record_audit(
            "leader_import_report_downloaded",
            "event",
            event.id,
            {"church_id": str(church.id), "issue_count": issue_count},
        )
        db.session.commit()
        response = make_response(output.getvalue().encode("utf-8-sig"))
        response.mimetype = "text/csv"
        response.headers["Content-Disposition"] = (
            'attachment; filename="bunotan-import-errors.csv"'
        )
        return response
    except ParticipantError as exc:
        db.session.rollback()
        if _is_ajax():
            return jsonify(error=str(exc)), 400
        flash(str(exc), "danger")
    except Exception:
        db.session.rollback()
        raise
    return redirect(url_for("leader.dashboard"))


@bp.post("/import/report-audit")
@leader_required
@rate_limit("leader-import-report-audit", 10, 3600, 3600)
def audit_import_report_download():
    require_church_scope(current_user.church_id)
    try:
        event_id = uuid.UUID(request.form.get("event_id", ""))
        issue_count = int(request.form.get("issue_count", ""))
    except (ValueError, TypeError):
        return jsonify(error="Invalid error report request"), 400
    event = db.session.get(Event, event_id)
    church = db.session.get(Church, current_user.church_id)
    if not event or not church:
        return jsonify(error="Invalid error report request"), 400
    if issue_count < 0 or issue_count > 2500:
        return jsonify(error="Invalid error report request"), 400
    record_audit(
        "leader_import_report_downloaded",
        "event",
        event.id,
        {"church_id": str(church.id), "issue_count": issue_count},
    )
    db.session.commit()
    return "", 204


@bp.get("/import-template/<file_format>")
@leader_required
def import_template(file_format):
    require_church_scope(current_user.church_id)
    if file_format == "csv":
        output = io.StringIO(newline="")
        csv.writer(output).writerow(IMPORT_HEADERS)
        response = make_response(output.getvalue().encode("utf-8-sig"))
        response.mimetype = "text/csv"
        response.headers["Content-Disposition"] = (
            'attachment; filename="bunotan-participant-template.csv"'
        )
        return response
    abort(404)


@bp.get("/paper-registration")
@leader_required
def paper_registration():
    require_church_scope(current_user.church_id)
    church = db.session.get(Church, current_user.church_id)
    if not church:
        abort(403)
    return render_template("leader/paper_registration.html", church=church)


def _render_dashboard(credentials=None, credentials_list=None):
    submissions = _own_submissions()
    events = Event.query.filter_by(status="registration_open").order_by(Event.name).all()
    return render_template(
        "leader/dashboard.html",
        submissions=submissions,
        events=events,
        credentials=credentials,
        credentials_list=credentials_list,
    )


def _own_submissions():
    return (
        Participant.query.filter_by(church_id=current_user.church_id)
        .order_by(Participant.created_at.desc())
        .all()
    )


def _get_import_context():
    try:
        event_id = uuid.UUID(request.form.get("event_id", ""))
    except (ValueError, TypeError):
        event_id = None
    event = db.session.get(Event, event_id) if event_id else None
    church = db.session.get(Church, current_user.church_id)
    if not event:
        raise ParticipantError("Select an available event")
    if event.status != "registration_open":
        raise ParticipantError("Registration is not open for the selected event")
    if not church or not church.active:
        raise ParticipantError("Your church account is not configured")
    require_church_scope(church.id)
    return event, church


def _require_import_consents():
    if request.form.get("privacy_consent") != "yes":
        raise ParticipantError("Confirm that the privacy notice was provided")
    if request.form.get("participation_confirmed") != "yes":
        raise ParticipantError(
            "Confirm that every participant agreed to participate"
        )


def _uploaded_rows():
    file_storage = request.files.get("file")
    if not file_storage or not file_storage.filename:
        raise ParticipantError("Upload a CSV file")
    return parse_import_file(file_storage)


def _is_ajax():
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _preview_response(rows):
    return {
        "rows": rows,
        "eligible_count": sum(row["eligible"] for row in rows),
        "error_count": sum(bool(row["errors"]) for row in rows),
        "duplicate_count": sum(bool(row["warnings"]) for row in rows),
    }


def _safe_csv_value(value):
    value = str(value or "")
    if value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value
