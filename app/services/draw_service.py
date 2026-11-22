from datetime import datetime, timezone

from sqlalchemy import and_, func, or_

from ..models import (
    Assignment,
    Church,
    Draw,
    DistributionBatch,
    Participant,
    RetrievalCredential,
    db,
)

from .bunotan import generate_assignments, validate_assignments, DrawError
from .audit import record_audit
from .participants import record_registration_status


def draw_preflight(event):
    unresolved = Participant.query.filter(
        Participant.event_id == event.id,
        Participant.status.in_(("pending", "under_review")),
    ).count()
    approved = Participant.query.filter_by(
        event_id=event.id, status="approved"
    ).count()
    inactive_churches = (
        Participant.query.join(Church, Participant.church_id == Church.id)
        .filter(
            Participant.event_id == event.id,
            Participant.status == "approved",
            Church.active.is_(False),
        )
        .count()
    )
    duplicate_groups = (
        db.session.query(
            Participant.church_id,
            Participant.name_key,
        )
        .filter(
            Participant.event_id == event.id,
            Participant.status.in_(("pending", "under_review", "approved")),
        )
        .group_by(Participant.church_id, Participant.name_key)
        .having(func.count(Participant.id) > 1)
        .count()
    )
    missing_credentials = (
        Participant.query.outerjoin(
            RetrievalCredential,
            RetrievalCredential.participant_id == Participant.id,
        )
        .filter(
            Participant.event_id == event.id,
            Participant.status == "approved",
            RetrievalCredential.participant_id.is_(None),
        )
        .count()
    )
    invalid_consents = Participant.query.filter(
        Participant.event_id == event.id,
        Participant.status == "approved",
        or_(
            Participant.participation_confirmed.is_(False),
            Participant.privacy_consent.is_(False),
            and_(
                Participant.is_minor.is_(True),
                Participant.guardian_consent.is_(False),
            ),
        ),
    ).count()
    finalized_draws = Draw.query.filter_by(
        event_id=event.id, status="finalized"
    ).count()

    checks = [
        {
            "key": "event_ready",
            "label": "Event is ready for drawing",
            "passed": event.status == "ready_for_drawing",
            "detail": f"Current status: {event.status.replace('_', ' ')}",
        },
        {
            "key": "registrations_reviewed",
            "label": "All registrations have been reviewed",
            "passed": unresolved == 0,
            "detail": f"{unresolved} pending or under-review registration(s)",
        },
        {
            "key": "approved_count",
            "label": "At least two participants are approved",
            "passed": approved >= 2,
            "detail": f"{approved} approved participant(s)",
        },
        {
            "key": "active_churches",
            "label": "Approved participants belong to active churches",
            "passed": inactive_churches == 0,
            "detail": f"{inactive_churches} approved participant(s) at inactive churches",
        },
        {
            "key": "unique_names",
            "label": "No active duplicate names remain",
            "passed": duplicate_groups == 0,
            "detail": f"{duplicate_groups} duplicate name group(s)",
        },
        {
            "key": "credentials",
            "label": "Every approved participant has retrieval credentials",
            "passed": missing_credentials == 0,
            "detail": f"{missing_credentials} missing credential(s)",
        },
        {
            "key": "consents",
            "label": "Required participant and guardian consent is recorded",
            "passed": invalid_consents == 0,
            "detail": f"{invalid_consents} approved participant record(s) missing required consent",
        },
        {
            "key": "no_active_draw",
            "label": "No finalized draw is already active",
            "passed": finalized_draws == 0,
            "detail": f"{finalized_draws} finalized draw(s) currently active",
        },
    ]
    return {
        "checks": checks,
        "passed": all(check["passed"] for check in checks),
        "approved_count": approved,
        "unresolved_count": unresolved,
    }


def run_draw(event):
    """Create a finalized draw version; an active version must be superseded explicitly."""
    if Draw.query.filter_by(event_id=event.id, status="finalized").first():
        raise DrawError("Supersede the active draw with a reason before rerunning")
    report = draw_preflight(event)
    if not report["passed"]:
        failures = [
            check["label"]
            for check in report["checks"]
            if not check["passed"]
        ]
        raise DrawError("Drawing preflight failed: " + "; ".join(failures))
    people = Participant.query.filter_by(event_id=event.id, status="approved").all()
    ids = [p.id for p in people]
    mapping = generate_assignments(ids)
    problems = validate_assignments(ids, mapping)
    if problems:
        raise DrawError("; ".join(problems))
    last = Draw.query.filter_by(event_id=event.id).order_by(Draw.version.desc()).first()
    try:
        draw = Draw(event_id=event.id, version=(last.version + 1 if last else 1),
                    participant_count=len(ids), validation_ok=True)
        db.session.add(draw)
        db.session.flush()
        db.session.add_all(Assignment(draw_id=draw.id, giver_id=g, recipient_id=r)
                           for g, r in mapping.items())
        event.status = "drawing_finalized"
        record_audit(
            "draw_finalized",
            "draw",
            draw.id,
            {"event_id": str(event.id), "version": draw.version},
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return draw


def cancel_draw(event, reason):
    reason = (reason or "").strip()
    if not reason or len(reason) > 2000:
        raise DrawError("A cancellation reason is required")
    if event.status != "drawing_finalized":
        raise DrawError("Only a finalized draw can be superseded before distribution")
    active_draw = (
        Draw.query.filter_by(event_id=event.id, status="finalized")
        .order_by(Draw.version.desc())
        .first()
    )
    if not active_draw:
        raise DrawError("No finalized draw is available to supersede")
    distributed_batches = DistributionBatch.query.filter_by(
        draw_id=active_draw.id, status="distributed"
    ).count()
    if distributed_batches:
        raise DrawError("A draw cannot be superseded after distribution has begun")
    try:
        prepared_batches = DistributionBatch.query.filter_by(
            draw_id=active_draw.id, status="prepared"
        ).all()
        for batch in prepared_batches:
            batch.status = "void"
        active_draw.status = "superseded"
        active_draw.cancel_reason = reason
        event.status = "ready_for_drawing"
        record_audit(
            "draw_superseded",
            "draw",
            active_draw.id,
            {
                "event_id": str(event.id),
                "version": active_draw.version,
                "reason": reason,
                "voided_prepared_batches": len(prepared_batches),
            },
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return active_draw


def withdraw_after_draw(event, participant, reason):
    reason = (reason or "").strip()
    if not reason or len(reason) > 2000:
        raise DrawError("A withdrawal reason is required")
    if event.status != "drawing_finalized":
        raise DrawError("The event does not have a finalized draw")
    if participant.event_id != event.id or participant.status != "approved":
        raise DrawError("Participant is not eligible for withdrawal")
    remaining = Participant.query.filter_by(
        event_id=event.id, status="approved"
    ).count() - 1
    if remaining < 2:
        raise DrawError("At least two approved participants must remain after withdrawal")
    active_draw = (
        Draw.query.filter_by(event_id=event.id, status="finalized")
        .order_by(Draw.version.desc())
        .first()
    )
    if not active_draw:
        raise DrawError("No finalized draw is available to supersede")
    if DistributionBatch.query.filter_by(
        draw_id=active_draw.id, status="distributed"
    ).count():
        raise DrawError("A draw cannot be superseded after distribution has begun")
    try:
        active_draw.status = "superseded"
        active_draw.cancel_reason = reason
        prepared_batches = DistributionBatch.query.filter_by(
            draw_id=active_draw.id, status="prepared"
        ).all()
        for batch in prepared_batches:
            batch.status = "void"
        previous_status = participant.status
        participant.status = "withdrawn"
        participant.withdrawal_reason = reason
        if participant.retrieval_credential:
            participant.retrieval_credential.revoked_at = datetime.now(timezone.utc)
        record_registration_status(participant, previous_status, reason)
        event.status = "ready_for_drawing"
        record_audit(
            "participant_withdrawn_after_draw",
            "participant",
            participant.id,
            {
                "event_id": str(event.id),
                "superseded_draw_version": active_draw.version,
                "reason": reason,
                "voided_prepared_batches": len(prepared_batches),
            },
        )
        return run_draw(event)
    except Exception:
        db.session.rollback()
        raise
