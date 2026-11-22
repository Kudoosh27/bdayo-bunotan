from ..models import (
    Draw,
    DistributionBatch,
    DistributionRecord,
    Event,
    Participant,
    db,
)

TRANSITIONS = {
    "draft": {"registration_open"},
    "registration_open": {"registration_closed"},
    "registration_closed": {"verification"},
    "verification": {"ready_for_drawing"},
    "ready_for_drawing": set(),
    "drawing_finalized": {"distribution"},
    "distribution": {"completed"},
    "completed": {"archived"},
    "archived": set(),
}


class EventTransitionError(ValueError):
    pass


def transition_event(event: Event, next_status: str):
    if next_status not in TRANSITIONS.get(event.status, set()):
        raise EventTransitionError("Invalid event status transition")
    if next_status == "ready_for_drawing":
        unresolved = Participant.query.filter(
            Participant.event_id == event.id,
            Participant.status.in_(("pending", "under_review")),
        ).count()
        approved = Participant.query.filter_by(
            event_id=event.id, status="approved"
        ).count()
        if unresolved:
            raise EventTransitionError(
                "Resolve every pending registration before drawing"
            )
        if approved < 2:
            raise EventTransitionError(
                "At least two approved participants are required before drawing"
            )
    if next_status == "distribution":
        active_draw = Draw.query.filter_by(
            event_id=event.id, status="finalized"
        ).first()
        if not active_draw or not active_draw.validation_ok:
            raise EventTransitionError(
                "Finalize and validate the district draw before distribution"
            )
    if next_status == "completed":
        pending_records = (
            DistributionRecord.query.join(DistributionBatch)
            .filter(
                DistributionBatch.status != "void",
                DistributionBatch.draw_id.in_(
                    db.session.query(Draw.id).filter(Draw.event_id == event.id)
                ),
                DistributionRecord.status == "pending",
            )
            .count()
        )
        if pending_records:
            raise EventTransitionError(
                "Resolve every pending distribution record before completing the event"
            )
    event.status = next_status
