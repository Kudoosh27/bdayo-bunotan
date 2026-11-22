import csv
import json
from io import BytesIO, StringIO

from sqlalchemy import and_, case, func
from sqlalchemy.orm import aliased

from ..models import (
    Assignment,
    AuditLog,
    Church,
    DistributionBatch,
    DistributionRecord,
    Draw,
    Event,
    Participant,
    RegistrationStatusHistory,
    db,
)
from .bunotan import validate_assignments
from .draw_service import draw_preflight


class ReportError(ValueError):
    pass


def build_event_reports(event_id):
    event = db.session.get(Event, event_id)
    if not event:
        raise ReportError("The selected event could not be found.")

    masterlist = (
        db.session.query(Participant, Church.name)
        .join(Church, Participant.church_id == Church.id)
        .filter(Participant.event_id == event.id)
        .order_by(Church.name, Participant.full_name, Participant.id)
        .all()
    )
    church_summary = (
        db.session.query(
            Church.name,
            func.count(Participant.id).label("total"),
            func.coalesce(
                func.sum(case((Participant.status == "approved", 1), else_=0)), 0
            ).label("approved"),
            func.coalesce(
                func.sum(
                    case(
                        (Participant.status.in_(("pending", "under_review")), 1),
                        else_=0,
                    )
                ),
                0,
            ).label("pending"),
            func.coalesce(
                func.sum(case((Participant.status == "rejected", 1), else_=0)), 0
            ).label("rejected"),
            func.coalesce(
                func.sum(case((Participant.status == "withdrawn", 1), else_=0)), 0
            ).label("withdrawn"),
        )
        .outerjoin(
            Participant,
            and_(
                Participant.church_id == Church.id,
                Participant.event_id == event.id,
            ),
        )
        .group_by(Church.id, Church.name)
        .order_by(Church.name)
        .all()
    )
    active_draw = (
        Draw.query.filter_by(event_id=event.id, status="finalized")
        .order_by(Draw.version.desc())
        .first()
    )
    validation = _drawing_validation(event, active_draw)
    distribution = (
        db.session.query(
            DistributionBatch,
            Church.name,
            Draw.version,
            func.count(DistributionRecord.id).label("record_count"),
            func.coalesce(
                func.sum(case((DistributionRecord.status == "delivered", 1), else_=0)),
                0,
            ).label("acknowledged_count"),
            func.coalesce(
                func.sum(case((DistributionRecord.status == "pending", 1), else_=0)),
                0,
            ).label("pending_count"),
        )
        .join(Draw, Draw.id == DistributionBatch.draw_id)
        .join(Church, Church.id == DistributionBatch.church_id)
        .outerjoin(
            DistributionRecord,
            DistributionRecord.batch_id == DistributionBatch.id,
        )
        .filter(Draw.event_id == event.id)
        .group_by(DistributionBatch.id, Church.name, Draw.version)
        .order_by(Draw.version.desc(), Church.name, DistributionBatch.version.desc())
        .all()
    )
    withdrawals = (
        db.session.query(RegistrationStatusHistory, Participant, Church.name)
        .join(Participant, Participant.id == RegistrationStatusHistory.participant_id)
        .join(Church, Church.id == Participant.church_id)
        .filter(
            Participant.event_id == event.id,
            RegistrationStatusHistory.new_status == "withdrawn",
        )
        .order_by(RegistrationStatusHistory.created_at.desc())
        .all()
    )
    return {
        "event": event,
        "masterlist": masterlist,
        "church_summary": church_summary,
        "validation": validation,
        "distribution": distribution,
        "withdrawals": withdrawals,
    }


def build_assignment_rows(event_id):
    event = db.session.get(Event, event_id)
    if not event:
        raise ReportError("The selected event could not be found.")
    draw = (
        Draw.query.filter_by(event_id=event.id, status="finalized")
        .order_by(Draw.version.desc())
        .first()
    )
    if not draw:
        raise ReportError("Assignment exports require an active validated draw.")
    validation = _active_draw_validation(event, draw)
    if not validation["passed"]:
        raise ReportError("Assignment exports require an active validated draw.")

    giver = aliased(Participant)
    recipient = aliased(Participant)
    rows = (
        db.session.query(giver.full_name, Church.name, recipient.full_name)
        .join(Assignment, Assignment.giver_id == giver.id)
        .join(recipient, Assignment.recipient_id == recipient.id)
        .join(Church, giver.church_id == Church.id)
        .filter(Assignment.draw_id == draw.id)
        .order_by(Church.name, giver.full_name, giver.id)
        .all()
    )
    return event, draw, rows


def build_audit_rows(limit=1000):
    return (
        AuditLog.query.order_by(AuditLog.created_at.desc())
        .limit(limit)
        .all()
    )


def csv_file(headers, rows):
    output = StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(headers)
    for row in rows:
        writer.writerow([_csv_value(value) for value in row])
    return BytesIO(output.getvalue().encode("utf-8-sig"))


def _drawing_validation(event, active_draw):
    if active_draw:
        validation = _active_draw_validation(event, active_draw)
        return {
            "passed": validation["passed"],
            "draw_version": active_draw.version,
            "checks": validation["checks"],
        }
    report = draw_preflight(event)
    return {
        "passed": report["passed"],
        "draw_version": None,
        "checks": report["checks"],
    }


def _active_draw_validation(event, draw):
    approved_ids = [
        row[0]
        for row in db.session.query(Participant.id)
        .filter_by(event_id=event.id, status="approved")
        .all()
    ]
    assignments = Assignment.query.filter_by(draw_id=draw.id).all()
    mapping = {assignment.giver_id: assignment.recipient_id for assignment in assignments}
    problems = validate_assignments(approved_ids, mapping)
    complete = len(mapping) == len(approved_ids)
    participant_count_valid = draw.participant_count == len(approved_ids)
    current_valid = complete and participant_count_valid and not problems
    validation_detail = "All approved participants have a valid assignment."
    if not participant_count_valid:
        validation_detail = "The draw participant count does not match the approved roster."
    elif not current_valid:
        validation_detail = "; ".join(problems) or "The draw does not cover every approved participant."
    return {
        "passed": bool(draw.validation_ok and current_valid),
        "checks": [
            {
                "label": "Active draw finalized",
                "passed": True,
                "detail": f"Version {draw.version}",
            },
            {
                "label": "Stored draw validation",
                "passed": bool(draw.validation_ok),
                "detail": f"{draw.participant_count} participant(s) recorded",
            },
            {
                "label": "Current assignment derangement",
                "passed": current_valid,
                "detail": validation_detail,
            },
        ],
    }


def _csv_value(value):
    if value is None:
        return ""
    if isinstance(value, dict):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, str) and value.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")):
        return "'" + value
    return value
