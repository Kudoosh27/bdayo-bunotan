import shutil
from pathlib import Path

from flask import current_app
from sqlalchemy import or_

from ..models import (
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
    RetrievalCredential,
    User,
    db,
)


DEMO_EVENT_NAME = "DEMO - District Youth Bunotan Sample"
DEMO_CHURCH_NAMES = (
    "DEMO - North Church",
    "DEMO - Central Church",
    "DEMO - South Church",
)


class DemoCleanupError(RuntimeError):
    pass


def clear_demo_data():
    event = Event.query.filter_by(name=DEMO_EVENT_NAME).first()
    if not event:
        return {"found": False, "deleted": {}, "retained_churches": []}

    participant_ids = [
        row[0]
        for row in db.session.query(Participant.id)
        .filter_by(event_id=event.id)
        .all()
    ]
    draw_ids = [
        row[0]
        for row in db.session.query(Draw.id).filter_by(event_id=event.id).all()
    ]
    assignment_ids = (
        [
            row[0]
            for row in db.session.query(Assignment.id)
            .filter(Assignment.draw_id.in_(draw_ids))
            .all()
        ]
        if draw_ids
        else []
    )
    batch_ids = (
        [
            row[0]
            for row in db.session.query(DistributionBatch.id)
            .filter(DistributionBatch.draw_id.in_(draw_ids))
            .all()
        ]
        if draw_ids
        else []
    )
    record_ids = (
        [
            row[0]
            for row in db.session.query(DistributionRecord.id)
            .filter(DistributionRecord.batch_id.in_(batch_ids))
            .all()
        ]
        if batch_ids
        else []
    )
    church_ids = {
        row[0]
        for row in db.session.query(Participant.church_id)
        .filter_by(event_id=event.id)
        .distinct()
        .all()
    }
    church_ids.update(
        row[0]
        for row in db.session.query(Church.id)
        .filter(Church.name.in_(DEMO_CHURCH_NAMES))
        .all()
    )
    packet_directories = _private_packet_directories(batch_ids)

    entity_ids = {
        str(value)
        for values in (
            [event.id],
            participant_ids,
            draw_ids,
            assignment_ids,
            batch_ids,
            record_ids,
            list(church_ids),
        )
        for value in values
    }
    deleted = {}
    try:
        if batch_ids:
            db.session.query(DistributionBatch).filter(
                DistributionBatch.id.in_(batch_ids)
            ).update(
                {DistributionBatch.replacement_of_id: None},
                synchronize_session=False,
            )
            deleted["distribution_records"] = (
                db.session.query(DistributionRecord)
                .filter(DistributionRecord.batch_id.in_(batch_ids))
                .delete(synchronize_session=False)
            )
        if assignment_ids or participant_ids:
            log_filter = []
            if assignment_ids:
                log_filter.append(
                    AssignmentAccessLog.assignment_id.in_(assignment_ids)
                )
            if participant_ids:
                log_filter.append(
                    AssignmentAccessLog.participant_id.in_(participant_ids)
                )
            deleted["assignment_access_logs"] = (
                db.session.query(AssignmentAccessLog)
                .filter(or_(*log_filter))
                .delete(synchronize_session=False)
            )
        if batch_ids:
            deleted["distribution_batches"] = (
                db.session.query(DistributionBatch)
                .filter(DistributionBatch.id.in_(batch_ids))
                .delete(synchronize_session=False)
            )
        if participant_ids:
            deleted["registration_history"] = (
                db.session.query(RegistrationStatusHistory)
                .filter(
                    RegistrationStatusHistory.participant_id.in_(participant_ids)
                )
                .delete(synchronize_session=False)
            )
            deleted["retrieval_credentials"] = (
                db.session.query(RetrievalCredential)
                .filter(RetrievalCredential.participant_id.in_(participant_ids))
                .delete(synchronize_session=False)
            )
        if assignment_ids:
            deleted["assignments"] = (
                db.session.query(Assignment)
                .filter(Assignment.id.in_(assignment_ids))
                .delete(synchronize_session=False)
            )
        if participant_ids:
            deleted["participants"] = (
                db.session.query(Participant)
                .filter(Participant.id.in_(participant_ids))
                .delete(synchronize_session=False)
            )
        if draw_ids:
            deleted["draws"] = (
                db.session.query(Draw)
                .filter(Draw.id.in_(draw_ids))
                .delete(synchronize_session=False)
            )
        deleted["audit_logs"] = (
            db.session.query(AuditLog)
            .filter(
                or_(
                    AuditLog.entity_id.in_(entity_ids),
                    AuditLog.details["event_id"].as_string() == str(event.id),
                )
            )
            .delete(synchronize_session=False)
        )
        deleted["events"] = (
            db.session.query(Event).filter_by(id=event.id).delete()
        )

        retained_churches = []
        deleted_churches = 0
        for church in Church.query.filter(Church.id.in_(church_ids)).all():
            is_referenced = (
                Participant.query.filter_by(church_id=church.id).first()
                or User.query.filter_by(church_id=church.id).first()
                or DistributionBatch.query.filter_by(church_id=church.id).first()
            )
            if is_referenced:
                retained_churches.append(church.name)
            else:
                db.session.delete(church)
                deleted_churches += 1
        deleted["churches"] = deleted_churches
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        raise DemoCleanupError(
            "Could not clear DEMO database records; the database changes were rolled back."
        ) from exc

    for directory in packet_directories:
        try:
            shutil.rmtree(directory)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise DemoCleanupError(
                "DEMO database records were removed, but a private packet directory "
                f"could not be deleted: {directory}"
            ) from exc

    return {
        "found": True,
        "deleted": deleted,
        "retained_churches": retained_churches,
    }


def _private_packet_directories(batch_ids):
    if not batch_ids:
        return []
    configured = current_app.config.get("PRIVATE_STORAGE_DIR")
    root = Path(configured) if configured else Path(current_app.instance_path) / "private"
    if not root.is_absolute():
        root = Path(current_app.instance_path) / root
    root = root.resolve()
    static_root = Path(current_app.static_folder).resolve()
    if root == static_root or static_root in root.parents:
        raise DemoCleanupError(
            "Private packet storage must be outside the static directory."
        )

    distribution_root = root / "distribution"
    directories = []
    for batch_id in batch_ids:
        candidate = distribution_root / str(batch_id)
        if candidate.is_symlink():
            raise DemoCleanupError(
                "A DEMO packet directory is a symbolic link; refusing to remove it."
            )
        resolved = candidate.resolve()
        if resolved.parent != distribution_root.resolve():
            raise DemoCleanupError("Invalid DEMO packet directory path.")
        directories.append(resolved)
    return directories
