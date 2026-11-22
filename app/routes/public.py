import uuid
from datetime import datetime, timezone

from flask import Blueprint, current_app, flash, render_template, request, url_for
from sqlalchemy import select

from ..models import (
    Assignment,
    AssignmentAccessLog,
    Church,
    Draw,
    Event,
    Participant,
    RetrievalCredential,
    db,
)
from ..services.audit import record_audit
from ..services.participants import ParticipantError, add_participant, get_registration_options
from ..services.pins import hash_pin, verify_pin
from ..services.rate_limits import (
    clear_rate_limit_attempt,
    is_rate_limit_locked,
    rate_limit,
    record_rate_limit_failure,
)

bp = Blueprint("public", __name__)
_DUMMY_PIN_HASH = hash_pin("not-a-real-participant-pin")


def _credential_not_expired(credential, now):
    if credential.expires_at is None:
        return True
    expires_at = credential.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at > now


@bp.get("/")
def home():
    return render_template("public/home.html")


@bp.get("/mechanics")
def mechanics():
    return render_template("public/mechanics.html")


@bp.get("/gift-guidelines")
def gift_guidelines():
    return render_template("public/gift_guidelines.html")


@bp.get("/faq")
def faq():
    return render_template("public/faq.html")


@bp.get("/privacy")
def privacy():
    return render_template(
        "public/privacy.html",
        notice_version=current_app.config["PRIVACY_NOTICE_VERSION"],
    )


@bp.get("/contact")
def contact():
    return render_template(
        "public/contact.html",
        contact_email=current_app.config.get("PUBLIC_CONTACT_EMAIL"),
    )


@bp.route("/register", methods=["GET", "POST"])
@rate_limit("registration", 10, 3600, 3600)
def register():
    events, churches = get_registration_options()
    credentials = None
    if request.method == "POST":
        try:
            event = db.session.get(Event, _form_uuid("event_id"))
            church = db.session.get(Church, _form_uuid("church_id"))
            if not event or not church:
                raise ParticipantError("Select an available event and church")
            guardian_consent = request.form.get("guardian_consent") == "yes"
            is_minor = request.form.get("is_minor") == "yes"
            privacy_consent = request.form.get("privacy_consent") == "yes"
            participation_confirmed = (
                request.form.get("participation_confirmed") == "yes"
            )
            participant, pin = add_participant(
                event,
                church,
                request.form.get("full_name", ""),
                guardian_consent=is_minor and guardian_consent,
                is_minor=is_minor,
                privacy_consent=privacy_consent,
                participation_confirmed=participation_confirmed,
                gift_preference=request.form.get("gift_preference", ""),
            )
            db.session.commit()
            credential = participant.retrieval_credential
            return render_template(
                "public/registration_confirmed.html",
                credentials={"reference": credential.reference, "pin": pin},
                participant=participant,
                event=event,
            )
        except ParticipantError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
        except Exception:
            db.session.rollback()
            raise
    return render_template(
        "public/register.html", events=events, churches=churches, credentials=credentials
    )


@bp.route("/lookup", methods=["GET", "POST"])
@rate_limit("assignment-lookup", 10, 60, 60)
def lookup():
    result = None
    if request.method == "POST":
        reference = request.form.get("reference", "").strip()
        pin = request.form.get("pin", "")
        now = datetime.now(timezone.utc)
        credential = RetrievalCredential.query.filter_by(reference=reference.upper()).first()
        participant = (
            db.session.get(Participant, credential.participant_id) if credential else None
        )
        normalized_reference = reference.upper()
        is_locked = is_rate_limit_locked("lookup-reference", normalized_reference)
        pin_valid = False
        if not is_locked:
            pin_input = pin[:6]
            hash_for_check = (
                credential.pin_hash
                if credential and len(pin) == 6
                else _DUMMY_PIN_HASH
            )
            pin_valid = verify_pin(
                hash_for_check,
                pin_input,
            )
            pin_valid = pin_valid and len(pin) == 6
        credential_active = bool(
            credential
            and credential.revoked_at is None
            and _credential_not_expired(credential, now)
        )
        if participant and pin_valid and not is_locked and credential_active:
            event = db.session.get(Event, participant.event_id)
            draw = (
                Draw.query.filter_by(event_id=participant.event_id, status="finalized")
                .order_by(Draw.version.desc())
                .first()
            )
            if event and event.status in {"distribution", "completed"} and draw:
                assignment = db.session.execute(
                    select(Assignment).where(
                        Assignment.draw_id == draw.id,
                        Assignment.giver_id == participant.id,
                    )
                ).scalar_one_or_none()
                recipient = (
                    db.session.get(Participant, assignment.recipient_id) if assignment else None
                )
                if recipient:
                    result = {"recipient": recipient.full_name, "event": event.name}
                    credential.last_used_at = now
                    db.session.add(
                        AssignmentAccessLog(
                            assignment_id=assignment.id,
                            participant_id=participant.id,
                            access_method="participant_lookup",
                        )
                    )
                    clear_rate_limit_attempt(
                        "lookup-reference", normalized_reference
                    )
                    record_audit("recipient_lookup_succeeded", "participant", participant.id)
                    db.session.commit()
                else:
                    db.session.rollback()
            else:
                record_audit("recipient_lookup_unavailable", "participant", participant.id)
                db.session.commit()
        else:
            _record_failed_lookup(normalized_reference)
        if result is None:
            flash("We could not verify those details or the result is not available.", "danger")
    return render_template("public/lookup.html", result=result)


def _record_failed_lookup(reference):
    lock_seconds = current_app.config["LOOKUP_LOCK_MINUTES"] * 60
    record_rate_limit_failure(
        "lookup-reference",
        reference,
        current_app.config["LOOKUP_MAX_ATTEMPTS"],
        lock_seconds,
        lock_seconds,
    )
    record_audit("recipient_lookup_failed", "participant")
    db.session.commit()


def _form_uuid(field):
    try:
        return uuid.UUID(request.form.get(field, ""))
    except (ValueError, TypeError):
        return None
