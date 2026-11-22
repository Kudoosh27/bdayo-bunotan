import csv
import io
import unicodedata

from flask import current_app, has_request_context
from sqlalchemy.exc import IntegrityError

from ..models import (
    Church,
    Event,
    Participant,
    RegistrationStatusHistory,
    RetrievalCredential,
    db,
)
from ..auth_state import current_user
from .audit import record_audit
from .pins import hash_pin, new_pin, new_reference
from .validators import clean_optional_text, parse_boolean, validate_participant_name


class ParticipantError(ValueError):
    pass


IMPORT_HEADERS = ("full_name", "is_minor", "guardian_consent")
IMPORT_MAX_ROWS = 500


def normalize_name(name):
    return " ".join(unicodedata.normalize("NFKC", name).strip().split()).casefold()


def record_registration_status(participant, previous_status, reason=None):
    actor_id = (
        current_user.id
        if has_request_context() and current_user.is_authenticated
        else None
    )
    db.session.add(
        RegistrationStatusHistory(
            participant_id=participant.id,
            previous_status=previous_status,
            new_status=participant.status,
            changed_by_id=actor_id,
            reason=reason,
        )
    )


def _new_credentials():
    for _ in range(5):
        reference = new_reference()
        if not RetrievalCredential.query.filter_by(reference=reference).first():
            return reference, new_pin()
    raise ParticipantError("Could not create registration credentials; please retry")


def add_participant(
    event,
    church,
    full_name,
    guardian_consent,
    *,
    is_minor=False,
    privacy_consent=True,
    participation_confirmed=False,
    gift_preference=None,
    source="public",
):
    if event.status != "registration_open":
        raise ParticipantError("Registration is not open")
    if not church.active:
        raise ParticipantError("Church is not active")
    if not privacy_consent:
        raise ParticipantError("Privacy notice consent is required")
    if not participation_confirmed:
        raise ParticipantError("Confirm the registration details and participation")
    if is_minor and not guardian_consent:
        raise ParticipantError("Guardian consent is required for a minor")
    name = validate_participant_name(full_name)
    if not name:
        raise ParticipantError("Enter a valid participant name")
    preference = clean_optional_text(gift_preference)
    if preference and len(preference) > 500:
        raise ParticipantError("Gift preference must be 500 characters or fewer")
    name_key = normalize_name(name)
    duplicate = Participant.query.filter(
        Participant.event_id == event.id,
        Participant.church_id == church.id,
        Participant.name_key == name_key,
        Participant.status.in_(("pending", "under_review", "approved")),
    ).first()
    if duplicate:
        raise ParticipantError("A participant with this name is already submitted for this church")
    reference, pin = _new_credentials()
    participant = Participant(
        event_id=event.id,
        church_id=church.id,
        full_name=name,
        name_key=name_key,
        status="pending",
        participation_confirmed=True,
        gift_preference=preference,
        guardian_consent=bool(guardian_consent) if is_minor else False,
        is_minor=bool(is_minor),
        privacy_consent=True,
        privacy_notice_version=current_app.config["PRIVACY_NOTICE_VERSION"],
    )
    try:
        with db.session.begin_nested():
            db.session.add(participant)
            db.session.flush()
            db.session.add(
                RetrievalCredential(
                    participant_id=participant.id,
                    reference=reference,
                    pin_hash=hash_pin(pin),
                )
            )
            db.session.add(
                RegistrationStatusHistory(
                    participant_id=participant.id,
                    previous_status=None,
                    new_status=participant.status,
                )
            )
            record_audit(
                "participant_submitted",
                "participant",
                participant.id,
                {"source": source},
            )
            db.session.flush()
    except IntegrityError as exc:
        raise ParticipantError(
            "Could not submit participant; check for a duplicate and retry"
        ) from exc
    return participant, pin


def get_registration_options():
    events = Event.query.filter_by(status="registration_open").order_by(Event.name).all()
    churches = Church.query.filter_by(active=True).order_by(Church.name).all()
    return events, churches


def parse_import_file(file_storage):
    filename = (file_storage.filename or "").lower()
    if not filename.endswith(".csv"):
        raise ParticipantError("Upload a CSV file")
    try:
        text = file_storage.stream.read().decode("utf-8-sig")
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
        headers = next(reader, None)
        if tuple(headers or ()) != IMPORT_HEADERS:
            raise ParticipantError(
                "Use the template headers in this order: "
                + ", ".join(IMPORT_HEADERS)
            )
        source_rows = []
        for row_number, values in enumerate(reader, start=2):
            if not any(str(value or "").strip() for value in values):
                continue
            if len(source_rows) == IMPORT_MAX_ROWS:
                raise ParticipantError(
                    f"The file may contain at most {IMPORT_MAX_ROWS} participants"
                )
            row_errors = []
            if len(values) != len(IMPORT_HEADERS):
                row_errors.append("The row must contain exactly three columns")
            values = (values + [""] * len(IMPORT_HEADERS))[:len(IMPORT_HEADERS)]
            source_rows.append(
                {
                    "row_number": row_number,
                    "full_name": values[0],
                    "is_minor": values[1],
                    "guardian_consent": values[2],
                    "source_errors": row_errors,
                }
            )
    except (UnicodeDecodeError, csv.Error) as exc:
        raise ParticipantError("Upload a valid UTF-8 CSV file") from exc
    if not source_rows:
        raise ParticipantError(
            "The file must contain between 1 and 500 participant rows"
        )
    return source_rows


def _parse_boolean(value, field, errors):
    parsed = parse_boolean(value)
    if parsed is None:
        errors.append(f"{field} must be yes or no")
    return parsed


def preview_leader_import(event, church, source_rows):
    rows = []
    for source_row in source_rows:
        errors = list(source_row.get("source_errors", []))
        name_value = source_row.get("full_name")
        name = validate_participant_name(name_value)
        if not name:
            errors.append("Enter a valid participant name (maximum 200 characters)")
        is_minor = _parse_boolean(source_row.get("is_minor"), "is_minor", errors)
        guardian_consent = _parse_boolean(
            source_row.get("guardian_consent"), "guardian_consent", errors
        )
        if is_minor and guardian_consent is False:
            errors.append("Guardian consent is required for a minor")
        rows.append(
            {
                "row_number": source_row["row_number"],
                "full_name": name,
                "is_minor": is_minor,
                "guardian_consent": guardian_consent,
                "errors": errors,
                "warnings": [],
                "eligible": False,
            }
        )

    name_keys = {
        normalize_name(row["full_name"])
        for row in rows
        if row["full_name"] and len(row["full_name"]) <= 200
    }
    existing_names = set()
    if name_keys:
        existing_names = {
            participant.name_key
            for participant in Participant.query.filter(
                Participant.event_id == event.id,
                Participant.church_id == church.id,
                Participant.name_key.in_(name_keys),
                Participant.status.in_(("pending", "under_review", "approved")),
            ).all()
        }

    seen_names = set()
    for row in rows:
        if row["errors"] or not row["full_name"]:
            row["status"] = "error"
            continue
        name_key = normalize_name(row["full_name"])
        if name_key in existing_names:
            row["warnings"].append(
                "A participant with this name is already submitted for this church"
            )
            row["status"] = "duplicate"
        elif name_key in seen_names:
            row["warnings"].append("This name appears more than once in the file")
            row["status"] = "duplicate"
        else:
            seen_names.add(name_key)
            row["eligible"] = True
            row["status"] = "ready"
    return rows


def add_leader_batch(event, church, rows):
    if len(rows) > IMPORT_MAX_ROWS:
        raise ParticipantError("The file may contain at most 500 participants")
    created = []
    for row in rows:
        if not row["eligible"]:
            continue
        try:
            participant, pin = add_participant(
                event,
                church,
                row["full_name"],
                row["guardian_consent"],
                is_minor=row["is_minor"],
                participation_confirmed=True,
                source="church_leader",
            )
            created.append((participant, pin))
            row["eligible"] = False
            row["status"] = "imported"
        except ParticipantError as exc:
            row["errors"].append(str(exc))
            row["status"] = "error"
    return created, rows
