import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import current_app, render_template
from sqlalchemy.orm import aliased

from ..models import (
    Assignment,
    Church,
    DistributionBatch,
    DistributionRecord,
    Draw,
    Event,
    Participant,
    RetrievalCredential,
    db,
)
from .audit import record_audit
from .pins import hash_pin, new_pin


class DistributionError(ValueError):
    pass


def generate_church_packet(event_id, church_id, created_by_id, reason=None):
    event = db.session.get(Event, event_id)
    church = db.session.get(Church, church_id)
    if not event or not church:
        raise DistributionError("The event or church could not be found.")
    if not church.active:
        raise DistributionError("Packets cannot be generated for an inactive church.")
    if event.status not in {"drawing_finalized", "distribution"}:
        raise DistributionError(
            "Packets can be prepared only for a finalized draw or during distribution."
        )
    draw = (
        Draw.query.filter_by(event_id=event.id, status="finalized")
        .order_by(Draw.version.desc())
        .first()
    )
    if not draw or not draw.validation_ok:
        raise DistributionError("There is no active validated draw for this event.")

    latest = (
        DistributionBatch.query.filter_by(draw_id=draw.id, church_id=church.id)
        .order_by(DistributionBatch.version.desc())
        .first()
    )
    replacement_reason = _clean_reason(reason)
    if latest and not replacement_reason:
        raise DistributionError("A reason is required to replace an existing packet.")
    if not latest and reason and not replacement_reason:
        raise DistributionError("A replacement reason must be 1 to 2000 characters.")

    giver = aliased(Participant)
    recipient = aliased(Participant)
    credential = aliased(RetrievalCredential)
    assignments = (
        db.session.query(Assignment, giver, recipient, credential)
        .join(giver, Assignment.giver_id == giver.id)
        .join(recipient, Assignment.recipient_id == recipient.id)
        .outerjoin(credential, credential.participant_id == giver.id)
        .filter(
            Assignment.draw_id == draw.id,
            giver.event_id == event.id,
            giver.church_id == church.id,
            giver.status == "approved",
        )
        .order_by(giver.full_name, giver.id)
        .all()
    )
    if not assignments:
        raise DistributionError("This church has no approved assignments in the active draw.")
    approved_count = Participant.query.filter_by(
        event_id=event.id, status="approved"
    ).count()
    assignment_count = Assignment.query.filter_by(draw_id=draw.id).count()
    if assignment_count != approved_count:
        raise DistributionError("The active draw does not cover every approved participant.")
    if any(row[3] is None or row[3].revoked_at is not None for row in assignments):
        raise DistributionError("Every giver must have an active retrieval credential.")

    root = private_storage_root()
    batch = DistributionBatch(
        draw_id=draw.id,
        church_id=church.id,
        created_by_id=created_by_id,
        version=(latest.version + 1 if latest else 1),
        replacement_of_id=latest.id if latest else None,
        replacement_reason=replacement_reason or None,
    )
    db.session.add(batch)
    db.session.flush()

    batch_folder = root / "distribution" / str(batch.id)
    relative_packet_path = Path("distribution") / str(batch.id) / "church-packet.html"
    cards = []
    try:
        for assignment, giver_person, recipient_person, retrieval in assignments:
            pin = new_pin()
            retrieval.pin_hash = hash_pin(pin)
            retrieval.revoked_at = None
            retrieval.expires_at = None
            record = DistributionRecord(
                batch_id=batch.id,
                assignment_id=assignment.id,
                status="pending",
            )
            db.session.add(record)
            db.session.flush()
            card_path = (
                Path("distribution")
                / str(batch.id)
                / "cards"
                / f"{record.id}.html"
            )
            record.card_path = card_path.as_posix()
            card = {
                "giver": giver_person.full_name,
                "recipient": recipient_person.full_name,
                "reference": retrieval.reference,
                "pin": pin,
            }
            cards.append(card)
            _write_private_file(
                root,
                card_path,
                render_assignment_card_html(card, event, church, batch),
            )

        packet_html = render_church_packet_html(cards, event, church, batch)
        _write_private_file(root, relative_packet_path, packet_html)
        batch.packet_path = relative_packet_path.as_posix()
        batch.packet_generated_at = _now()
        if latest:
            latest.status = "void"

        record_audit(
            "distribution_packet_generated",
            "distribution_batch",
            batch.id,
            {
                "event_id": str(event.id),
                "draw_version": draw.version,
                "church_id": str(church.id),
                "card_count": len(cards),
                "replacement_of_id": str(latest.id) if latest else None,
            },
        )
        if latest:
            record_audit(
                "distribution_packet_replaced",
                "distribution_batch",
                latest.id,
                {
                    "replacement_batch_id": str(batch.id),
                    "reason": replacement_reason,
                },
            )
        db.session.commit()
    except Exception:
        db.session.rollback()
        if batch_folder.exists():
            shutil.rmtree(batch_folder)
        raise
    return batch


def render_assignment_card_html(card, event, church, batch):
    return render_church_packet_html([card], event, church, batch)


def render_church_packet_html(cards, event, church, batch):
    if not cards:
        raise DistributionError("A church packet must contain at least one assignment.")
    html = render_template(
        "admin/print_cards.html",
        cards=cards,
        event=event,
        church=church,
        batch=batch,
        single_card=len(cards) == 1,
    )
    return html.encode("utf-8")
def mark_packet_printed(batch_id):
    batch = _get_active_batch(batch_id)
    if batch.status not in {"prepared", "distributed"}:
        raise DistributionError("This packet cannot be marked as printed.")
    batch.print_count += 1
    batch.printed_at = _now()
    record_audit(
        "distribution_packet_printed",
        "distribution_batch",
        batch.id,
        {"print_count": batch.print_count},
    )
    db.session.commit()
    return batch


def mark_packet_distributed(batch_id):
    batch = _get_active_batch(batch_id)
    if batch.status != "prepared":
        raise DistributionError("Only a prepared packet can be marked as distributed.")
    event = db.session.get(Event, batch.draw.event_id)
    if not event or event.status != "distribution":
        raise DistributionError("The event must be in distribution before packet handoff.")
    if not batch.printed_at:
        raise DistributionError("Record printing before marking a packet distributed.")
    batch.status = "distributed"
    batch.distributed_at = _now()
    record_audit("distribution_packet_distributed", "distribution_batch", batch.id)
    db.session.commit()
    return batch


def acknowledge_distribution_record(record_id, expected_batch_id=None):
    record = db.session.get(DistributionRecord, record_id)
    if not record:
        raise DistributionError("The distribution record could not be found.")
    if expected_batch_id is not None and record.batch_id != expected_batch_id:
        raise DistributionError("The assignment does not belong to this packet.")
    batch = _get_active_batch(record.batch_id)
    if batch.status != "distributed":
        raise DistributionError("A packet must be distributed before acknowledgment.")
    if record.status != "pending":
        raise DistributionError("This assignment has already been acknowledged.")
    acknowledged_at = _now()
    record.status = "delivered"
    record.delivered_at = acknowledged_at
    record.acknowledged_at = acknowledged_at
    outstanding = DistributionRecord.query.filter_by(
        batch_id=batch.id, status="pending"
    ).count()
    if outstanding == 0:
        batch.acknowledged_at = acknowledged_at
    record_audit(
        "distribution_assignment_acknowledged",
        "distribution_record",
        record.id,
        {"batch_id": str(batch.id)},
    )
    db.session.commit()
    return record


def get_packet_file(batch_id, record_id=None):
    batch = _get_active_batch(batch_id)
    relative_path = batch.packet_path
    if record_id is not None:
        record = db.session.get(DistributionRecord, record_id)
        if not record or record.batch_id != batch.id:
            raise DistributionError("The assignment card could not be found.")
        relative_path = record.card_path
    if not relative_path:
        raise DistributionError("The private packet HTML is unavailable.")
    root = private_storage_root()
    path = (root / relative_path).resolve()
    if root not in path.parents or not path.is_file():
        raise DistributionError("The private packet file is unavailable.")
    return path


def private_storage_root():
    configured = current_app.config.get("PRIVATE_STORAGE_DIR")
    root = Path(configured) if configured else Path(current_app.instance_path) / "private"
    if not root.is_absolute():
        root = Path(current_app.instance_path) / root
    root = root.resolve()
    static_root = Path(current_app.static_folder).resolve()
    if root == static_root or static_root in root.parents:
        raise DistributionError("Private packet storage must be outside the static directory.")
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DistributionError("Private packet storage is not available.") from exc
    return root


def _get_active_batch(batch_id):
    batch = db.session.get(DistributionBatch, batch_id)
    if not batch:
        raise DistributionError("The distribution packet could not be found.")
    if batch.status == "void" or batch.draw.status != "finalized":
        raise DistributionError("This packet has been invalidated and is no longer available.")
    return batch


def _write_private_file(root, relative_path, content):
    target = (root / relative_path).resolve()
    if root not in target.parents:
        raise DistributionError("Invalid private packet file path.")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(
            str(temporary), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
        )
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
        os.replace(temporary, target)
    except OSError as exc:
        if temporary.exists():
            temporary.unlink()
        raise DistributionError("Could not save the private packet file.") from exc


def _clean_reason(reason):
    if reason is None:
        return ""
    cleaned = " ".join(reason.split())
    return cleaned if 1 <= len(cleaned) <= 2000 else ""


def _now():
    return datetime.now(timezone.utc)
