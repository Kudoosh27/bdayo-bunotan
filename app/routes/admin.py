from decimal import Decimal, InvalidOperation
import uuid

from flask import (
    Blueprint,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from sqlalchemy import false, func, tuple_
from sqlalchemy.exc import IntegrityError

from ..models import (
    Assignment,
    AuditLog,
    Church,
    Draw,
    DistributionBatch,
    DistributionRecord,
    Event,
    Participant,
    RegistrationStatusHistory,
    RetrievalCredential,
    User,
    db,
)
from ..auth_state import current_user
from ..services.access import admin_required
from ..services.audit import record_audit
from ..services.bunotan import DrawError
from ..services.draw_service import (
    cancel_draw,
    draw_preflight as build_draw_preflight,
    run_draw,
    withdraw_after_draw,
)
from ..services.events import EventTransitionError, transition_event
from ..services.participants import normalize_name, record_registration_status
from ..services.pins import hash_pin, new_pin, new_reference
from ..services.distribution import (
    DistributionError,
    acknowledge_distribution_record,
    generate_church_packet,
    get_packet_file,
    mark_packet_distributed,
    mark_packet_printed,
)
from ..services.reports import (
    ReportError,
    build_assignment_rows,
    build_audit_rows,
    build_event_reports,
    csv_file,
)
from ..services.rate_limits import clear_rate_limit_attempt

bp = Blueprint("admin", __name__)
PARTICIPANT_STATUSES = {"pending", "under_review", "approved", "rejected", "withdrawn"}
ACTIVE_PARTICIPANT_STATUSES = {"pending", "under_review", "approved"}


@bp.get("/health")
@admin_required
def health():
    return jsonify(
        database_dialect=db.engine.dialect.name,
        participant_count=Participant.query.count(),
    )


@bp.get("/")
@admin_required
def dashboard():
    duplicate_groups = _duplicate_groups_query().all()
    church_registrations = (
        db.session.query(Church.name, func.count(Participant.id))
        .outerjoin(Participant, Participant.church_id == Church.id)
        .group_by(Church.id, Church.name)
        .order_by(func.count(Participant.id).desc(), Church.name)
        .limit(10)
        .all()
    )
    status_breakdown = (
        db.session.query(Participant.status, func.count(Participant.id))
        .group_by(Participant.status)
        .order_by(Participant.status)
        .all()
    )
    active_distribution_records = DistributionRecord.query.join(
        DistributionBatch
    ).filter(DistributionBatch.status != "void")
    distribution_total = active_distribution_records.count()
    distribution_acknowledged = active_distribution_records.filter(
        DistributionRecord.acknowledged_at.is_not(None)
    ).count()
    metrics = {
        "participants": Participant.query.count(),
        "pending": Participant.query.filter(
            Participant.status.in_(("pending", "under_review"))
        ).count(),
        "approved": Participant.query.filter_by(status="approved").count(),
        "active_churches": Church.query.filter_by(active=True).count(),
        "open_events": Event.query.filter_by(status="registration_open").count(),
        "duplicates": sum(group[3] for group in duplicate_groups),
    }
    events = Event.query.order_by(Event.created_at.desc()).all()
    return render_template(
        "admin/dashboard.html",
        events=events,
        metrics=metrics,
        chart_data={
            "churches": {
                "labels": [row[0] for row in church_registrations],
                "values": [row[1] for row in church_registrations],
            },
            "statuses": {
                "labels": [row[0].replace("_", " ").title() for row in status_breakdown],
                "values": [row[1] for row in status_breakdown],
            },
            "distribution": {
                "labels": ["Acknowledged", "Awaiting acknowledgment"],
                "values": [
                    distribution_acknowledged,
                    distribution_total - distribution_acknowledged,
                ],
            },
        },
    )


@bp.post("/events")
@admin_required
def create_event():
    name = _clean_text(request.form.get("name"), 200)
    if not name:
        _audit_rejected(
            "event_creation_rejected", "event", details={"reason_code": "invalid_name"}
        )
        flash("Enter an event name (maximum 200 characters).", "danger")
        return redirect(url_for("admin.dashboard"))
    try:
        budget = _parse_budget(request.form.get("gift_budget"))
    except ValueError as exc:
        _audit_rejected(
            "event_creation_rejected", "event", details={"reason_code": "invalid_budget"}
        )
        flash(str(exc), "danger")
        return redirect(url_for("admin.dashboard"))
    event = Event(name=name, gift_budget=budget)
    db.session.add(event)
    db.session.flush()
    record_audit(
        "event_created",
        "event",
        event.id,
        {"name": name, "gift_budget": str(budget) if budget is not None else None},
    )
    db.session.commit()
    flash("Event created.", "success")
    return redirect(url_for("admin.dashboard"))


@bp.post("/events/<uuid:event_id>/settings")
@admin_required
def update_event_settings(event_id):
    event = db.session.get(Event, event_id)
    if not event:
        abort(404)
    name = _clean_text(request.form.get("name"), 200)
    try:
        budget = _parse_budget(request.form.get("gift_budget"))
    except ValueError as exc:
        _audit_rejected(
            "event_settings_rejected",
            "event",
            event.id,
            {"reason_code": "invalid_budget"},
        )
        flash(str(exc), "danger")
        return redirect(url_for("admin.dashboard") + "#events")
    if not name:
        _audit_rejected(
            "event_settings_rejected",
            "event",
            event.id,
            {"reason_code": "invalid_name"},
        )
        flash("Enter an event name (maximum 200 characters).", "danger")
        return redirect(url_for("admin.dashboard") + "#events")
    previous = {"name": event.name, "gift_budget": _decimal_text(event.gift_budget)}
    event.name = name
    event.gift_budget = budget
    record_audit(
        "event_settings_updated",
        "event",
        event.id,
        {
            "before": previous,
            "after": {"name": name, "gift_budget": _decimal_text(budget)},
        },
    )
    db.session.commit()
    flash("Event settings updated.", "success")
    return redirect(url_for("admin.dashboard") + "#events")


@bp.post("/events/<uuid:event_id>/transition")
@admin_required
def transition(event_id):
    event = db.session.get(Event, event_id)
    if not event:
        abort(404)
    next_status = request.form.get("status", "")
    previous = event.status
    try:
        transition_event(event, next_status)
    except EventTransitionError as exc:
        db.session.rollback()
        _audit_rejected(
            "event_status_change_rejected",
            "event",
            event.id,
            {"from": previous, "requested": next_status},
        )
        flash(str(exc), "danger")
        return redirect(url_for("admin.dashboard") + "#events")
    record_audit(
        "event_status_changed",
        "event",
        event.id,
        {"from": previous, "to": event.status},
    )
    db.session.commit()
    flash("Event status updated.", "success")
    return redirect(url_for("admin.dashboard") + "#events")


@bp.post("/events/<uuid:event_id>/draw")
@admin_required
def draw(event_id):
    event = db.session.get(Event, event_id)
    if not event:
        abort(404)
    if Draw.query.filter_by(event_id=event.id, status="finalized").first():
        _audit_rejected(
            "draw_finalization_rejected",
            "event",
            event.id,
            {"reason_code": "already_finalized"},
        )
        flash(
            "The draw is locked after finalization; supersede it with a reason before rerunning.",
            "danger",
        )
        return redirect(url_for("admin.draw_preflight", event_id=event.id))
    if request.form.get("pre_draw_confirmed") != "yes":
        _audit_rejected(
            "draw_finalization_rejected",
            "event",
            event.id,
            {"reason_code": "pre_draw_checklist_not_confirmed"},
        )
        report = build_draw_preflight(event)
        flash("Confirm the pre-draw checklist before running the draw.", "danger")
        return (
            render_template(
                "admin/draw.html",
                event=event,
                report=report,
                active_draw=None,
                draw_history=[],
            ),
            400,
        )
    report = build_draw_preflight(event)
    if not report["passed"]:
        _audit_rejected(
            "draw_finalization_rejected",
            "event",
            event.id,
            {"reason_code": "preflight_failed"},
        )
        flash("Resolve the failed checklist items before running the draw.", "danger")
        return render_template(
            "admin/draw.html",
            event=event,
            report=report,
            active_draw=None,
            draw_history=[],
        ), 400
    try:
        finalized = run_draw(event)
    except DrawError as exc:
        db.session.rollback()
        _audit_rejected(
            "draw_finalization_rejected",
            "event",
            event.id,
            {"reason_code": "draw_engine_rejected"},
        )
        flash(str(exc), "danger")
        return redirect(url_for("admin.draw_preflight", event_id=event.id))
    flash(f"District draw version {finalized.version} was finalized.", "success")
    return redirect(url_for("admin.draw_preflight", event_id=event.id))


@bp.get("/events/<uuid:event_id>/draw")
@admin_required
def draw_preflight(event_id):
    event = db.session.get(Event, event_id)
    if not event:
        abort(404)
    active_draw = (
        Draw.query.filter_by(event_id=event.id, status="finalized")
        .order_by(Draw.version.desc())
        .first()
    )
    draw_history = (
        Draw.query.filter_by(event_id=event.id)
        .order_by(Draw.version.desc())
        .all()
    )
    return render_template(
        "admin/draw.html",
        event=event,
        report=build_draw_preflight(event),
        active_draw=active_draw,
        draw_history=draw_history,
    )


@bp.post("/events/<uuid:event_id>/draw/cancel")
@admin_required
def cancel_finalized_draw(event_id):
    event = db.session.get(Event, event_id)
    if not event:
        abort(404)
    if request.form.get("confirm_supersede") != "yes":
        _audit_rejected(
            "draw_supersede_rejected",
            "event",
            event.id,
            {"reason_code": "supersede_not_confirmed"},
        )
        flash("Confirm superseding the finalized draw.", "danger")
        return redirect(url_for("admin.draw_preflight", event_id=event.id))
    reason = _clean_text(request.form.get("reason"), 2000)
    if not reason:
        _audit_rejected(
            "draw_supersede_rejected",
            "event",
            event.id,
            {"reason_code": "missing_reason"},
        )
        flash("A reason is required to supersede the finalized draw.", "danger")
        return redirect(url_for("admin.draw_preflight", event_id=event.id))
    try:
        draw = cancel_draw(event, reason)
    except DrawError as exc:
        db.session.rollback()
        _audit_rejected(
            "draw_supersede_rejected",
            "event",
            event.id,
            {"reason_code": "draw_not_cancellable"},
        )
        flash(str(exc), "danger")
        return redirect(url_for("admin.draw_preflight", event_id=event.id))
    flash(
        f"Draw version {draw.version} was superseded and prepared distribution packets were voided. "
        "Review the checklist before a new draw.",
        "success",
    )
    return redirect(url_for("admin.draw_preflight", event_id=event.id))


@bp.get("/events/<uuid:event_id>/distribution")
@admin_required
def distribution_control(event_id):
    event = db.session.get(Event, event_id)
    if not event:
        abort(404)
    active_draw = (
        Draw.query.filter_by(event_id=event.id, status="finalized")
        .order_by(Draw.version.desc())
        .first()
    )
    churches = []
    latest_batches = {}
    records_by_batch = {}
    if active_draw:
        churches = (
            Church.query.join(Participant, Participant.church_id == Church.id)
            .join(Assignment, Assignment.giver_id == Participant.id)
            .filter(
                Assignment.draw_id == active_draw.id,
                Participant.event_id == event.id,
                Participant.status == "approved",
            )
            .distinct()
            .order_by(Church.name)
            .all()
        )
        batches = (
            DistributionBatch.query.filter_by(draw_id=active_draw.id)
            .order_by(DistributionBatch.version.desc())
            .all()
        )
        for batch in batches:
            latest_batches.setdefault(batch.church_id, batch)
        if latest_batches:
            record_rows = (
                db.session.query(DistributionRecord, Participant)
                .join(Assignment, Assignment.id == DistributionRecord.assignment_id)
                .join(Participant, Participant.id == Assignment.giver_id)
                .filter(
                    DistributionRecord.batch_id.in_(
                        [batch.id for batch in latest_batches.values()]
                    )
                )
                .all()
            )
            for record, giver in record_rows:
                records_by_batch.setdefault(record.batch_id, []).append(
                    {"record": record, "giver": giver}
                )
    return render_template(
        "admin/distribution.html",
        event=event,
        active_draw=active_draw,
        churches=churches,
        latest_batches=latest_batches,
        records_by_batch=records_by_batch,
    )


@bp.post("/events/<uuid:event_id>/distribution/churches/<uuid:church_id>/generate")
@admin_required
def generate_distribution_packet(event_id, church_id):
    try:
        batch = generate_church_packet(
            event_id, church_id, current_user.id, request.form.get("reason")
        )
    except DistributionError as exc:
        _audit_rejected(
            "distribution_packet_generation_rejected",
            "church",
            church_id,
            {"event_id": str(event_id), "reason": str(exc)},
        )
        flash(str(exc), "danger")
        return redirect(url_for("admin.distribution_control", event_id=event_id))
    flash(
        f"Church packet version {batch.version} generated. Participant PINs were rotated for this packet.",
        "success",
    )
    return redirect(url_for("admin.distribution_control", event_id=event_id))


@bp.get("/distribution/batches/<uuid:batch_id>/pdf")
@admin_required
def download_distribution_packet(batch_id):
    batch = db.session.get(DistributionBatch, batch_id)
    try:
        path = get_packet_file(batch_id)
    except DistributionError as exc:
        _audit_rejected(
            "distribution_packet_download_rejected",
            "distribution_batch",
            batch_id,
            {"reason": str(exc)},
        )
        abort(404)
    record_audit(
        "distribution_packet_downloaded",
        "distribution_batch",
        batch.id,
        {"version": batch.version},
    )
    db.session.commit()
    response = send_file(path, mimetype="text/html")
    response.headers["Cache-Control"] = "private, no-store"
    return response


@bp.get("/distribution/batches/<uuid:batch_id>/records/<uuid:record_id>/pdf")
@admin_required
def download_distribution_card(batch_id, record_id):
    batch = db.session.get(DistributionBatch, batch_id)
    try:
        path = get_packet_file(batch_id, record_id)
    except DistributionError as exc:
        _audit_rejected(
            "distribution_card_download_rejected",
            "distribution_batch",
            batch_id,
            {"reason": str(exc)},
        )
        abort(404)
    record_audit(
        "distribution_card_downloaded",
        "distribution_record",
        record_id,
        {"batch_id": str(batch.id)},
    )
    db.session.commit()
    response = send_file(path, mimetype="text/html")
    response.headers["Cache-Control"] = "private, no-store"
    return response


@bp.post("/distribution/batches/<uuid:batch_id>/printed")
@admin_required
def distribution_packet_printed(batch_id):
    try:
        batch = mark_packet_printed(batch_id)
    except DistributionError as exc:
        _audit_rejected(
            "distribution_packet_print_rejected",
            "distribution_batch",
            batch_id,
            {"reason": str(exc)},
        )
        flash(str(exc), "danger")
        return _distribution_redirect(batch_id)
    flash("Packet printing recorded.", "success")
    return _distribution_redirect(batch.id)


@bp.post("/distribution/batches/<uuid:batch_id>/distributed")
@admin_required
def distribution_packet_distributed(batch_id):
    try:
        batch = mark_packet_distributed(batch_id)
    except DistributionError as exc:
        _audit_rejected(
            "distribution_packet_distribution_rejected",
            "distribution_batch",
            batch_id,
            {"reason": str(exc)},
        )
        flash(str(exc), "danger")
        return _distribution_redirect(batch_id)
    flash("Packet distribution recorded. Record acknowledgments as envelopes are received.", "success")
    return _distribution_redirect(batch.id)


@bp.post("/distribution/batches/<uuid:batch_id>/records/<uuid:record_id>/acknowledge")
@admin_required
def distribution_record_acknowledged(batch_id, record_id):
    try:
        record = acknowledge_distribution_record(record_id, batch_id)
    except DistributionError as exc:
        _audit_rejected(
            "distribution_acknowledgment_rejected",
            "distribution_record",
            record_id,
            {"batch_id": str(batch_id), "reason": str(exc)},
        )
        flash(str(exc), "danger")
        return _distribution_redirect(batch_id)
    flash("Assignment acknowledgment recorded.", "success")
    return _distribution_redirect(batch_id)


@bp.get("/churches")
@admin_required
def churches():
    rows = Church.query.order_by(Church.name).all()
    leader_counts = dict(
        db.session.query(User.church_id, func.count(User.id))
        .filter(User.role == "leader")
        .group_by(User.church_id)
        .all()
    )
    return render_template(
        "admin/churches.html", churches=rows, leader_counts=leader_counts
    )


@bp.post("/churches")
@admin_required
def create_church():
    name = _clean_text(request.form.get("name"), 200)
    if not name:
        _audit_rejected(
            "church_creation_rejected",
            "church",
            details={"reason_code": "invalid_name"},
        )
        flash("Enter a church name (maximum 200 characters).", "danger")
        return redirect(url_for("admin.churches"))
    church = Church(name=name)
    db.session.add(church)
    try:
        db.session.flush()
        record_audit("church_created", "church", church.id, {"name": name})
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        _audit_rejected(
            "church_creation_rejected",
            "church",
            details={"reason_code": "duplicate_name"},
        )
        flash("That church name is already in use.", "danger")
        return redirect(url_for("admin.churches"))
    except Exception:
        db.session.rollback()
        raise
    flash("Church created.", "success")
    return redirect(url_for("admin.churches"))


@bp.post("/churches/<uuid:church_id>/update")
@admin_required
def update_church(church_id):
    church = db.session.get(Church, church_id)
    if not church:
        abort(404)
    name = _clean_text(request.form.get("name"), 200)
    if not name:
        _audit_rejected(
            "church_update_rejected",
            "church",
            church.id,
            {"reason_code": "invalid_name"},
        )
        flash("Enter a church name (maximum 200 characters).", "danger")
        return redirect(url_for("admin.churches"))
    active = request.form.get("active") == "yes"
    previous = {"name": church.name, "active": church.active}
    church.name = name
    church.active = active
    disabled_leaders = 0
    if not active:
        leaders = User.query.filter_by(church_id=church.id, role="leader").all()
        for leader in leaders:
            if leader.active:
                leader.active = False
                disabled_leaders += 1
    try:
        db.session.flush()
        record_audit(
            "church_updated",
            "church",
            church.id,
            {
                "before": previous,
                "after": {"name": name, "active": active},
                "leaders_disabled": disabled_leaders,
            },
        )
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        _audit_rejected(
            "church_update_rejected",
            "church",
            church.id,
            {"reason_code": "duplicate_name"},
        )
        flash("That church name is already in use.", "danger")
        return redirect(url_for("admin.churches"))
    except Exception:
        db.session.rollback()
        raise
    flash(
        "Church updated. Its leader accounts remain disabled until an administrator re-enables them."
        if not active
        else "Church updated.",
        "success",
    )
    return redirect(url_for("admin.churches"))


@bp.get("/users")
@admin_required
def users():
    rows = User.query.order_by(User.role, User.username).all()
    churches = Church.query.filter_by(active=True).order_by(Church.name).all()
    return render_template("admin/users.html", users=rows, churches=churches)


@bp.post("/users")
@admin_required
def create_leader():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    password_confirmation = request.form.get("password_confirmation", "")
    try:
        church_id = uuid.UUID(request.form.get("church_id", ""))
    except (ValueError, TypeError):
        church_id = None

    reason = None
    church = db.session.get(Church, church_id) if church_id else None
    if not username or len(username) > 120:
        reason = "invalid_username"
        message = "Username must contain 1 to 120 characters."
    elif User.query.filter_by(username=username).first():
        reason = "username_in_use"
        message = "That username is already in use."
    elif not church or not church.active:
        reason = "invalid_church"
        message = "Select an active church for this leader."
    elif len(password) < 12:
        reason = "password_too_short"
        message = "Password must be at least 12 characters."
    elif len(password) > 1024:
        reason = "password_too_long"
        message = "Password must be 1024 characters or fewer."
    elif password != password_confirmation:
        reason = "password_mismatch"
        message = "The password confirmation does not match."
    if reason:
        _audit_rejected(
            "leader_creation_rejected",
            "user",
            details={"reason_code": reason},
        )
        flash(message, "danger")
        return redirect(url_for("admin.users"))

    user = User(username=username, role="leader", church_id=church.id)
    user.set_password(password)
    try:
        db.session.add(user)
        db.session.flush()
        record_audit(
            "user_created",
            "user",
            user.id,
            {"role": "leader", "church_id": str(church.id), "source": "admin_ui"},
        )
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        _audit_rejected(
            "leader_creation_rejected",
            "user",
            details={"reason_code": "username_in_use"},
        )
        flash("That username is already in use.", "danger")
        return redirect(url_for("admin.users"))
    except Exception:
        db.session.rollback()
        raise

    flash("Church leader account created.", "success")
    return redirect(url_for("admin.users"))


@bp.post("/users/<uuid:user_id>/access")
@admin_required
def update_user_access(user_id):
    user = db.session.get(User, user_id)
    if not user:
        abort(404)
    previous = {"active": user.active, "church_id": str(user.church_id) if user.church_id else None}
    active = request.form.get("active") == "yes"
    if user.role == "leader":
        try:
            church_id = uuid.UUID(request.form.get("church_id", ""))
        except (ValueError, TypeError):
            _audit_rejected(
                "user_access_update_rejected",
                "user",
                user.id,
                {"reason_code": "invalid_church"},
            )
            flash("Select an active church for this leader.", "danger")
            return redirect(url_for("admin.users"))
        church = db.session.get(Church, church_id)
        if not church or not church.active:
            _audit_rejected(
                "user_access_update_rejected",
                "user",
                user.id,
                {"reason_code": "inactive_church"},
            )
            flash("Select an active church for this leader.", "danger")
            return redirect(url_for("admin.users"))
        user.church_id = church.id
        if active and not church.active:
            flash("A leader cannot be enabled for an inactive church.", "danger")
            return redirect(url_for("admin.users"))
    user.active = active
    record_audit(
        "user_access_updated",
        "user",
        user.id,
        {
            "before": previous,
            "after": {
                "active": user.active,
                "church_id": str(user.church_id) if user.church_id else None,
            },
        },
    )
    db.session.commit()
    flash("Staff access updated.", "success")
    return redirect(url_for("admin.users"))


@bp.get("/participants")
@admin_required
def all_participants():
    return _participant_search()


@bp.get("/events/<uuid:event_id>/participants")
@admin_required
def participants(event_id):
    event = db.session.get(Event, event_id)
    if not event:
        abort(404)
    return _participant_search(event)


def _participant_search(forced_event=None):
    events = Event.query.order_by(Event.name).all()
    churches = Church.query.order_by(Church.name).all()
    event_id = forced_event.id if forced_event else None
    if not forced_event and request.args.get("event_id"):
        try:
            event_id = uuid.UUID(request.args["event_id"])
        except (ValueError, TypeError):
            abort(400)
    event = forced_event or (db.session.get(Event, event_id) if event_id else None)
    if event_id and not event:
        abort(400)
    status = request.args.get("status", "")
    if status and status not in PARTICIPANT_STATUSES | {"awaiting_review"}:
        abort(400)
    church_id = None
    if request.args.get("church_id"):
        try:
            church_id = uuid.UUID(request.args["church_id"])
        except (ValueError, TypeError):
            abort(400)
        if not db.session.get(Church, church_id):
            abort(400)
    search = request.args.get("q", "").strip()[:120]
    duplicates_only = request.args.get("duplicates") == "1"
    duplicate_groups = _duplicate_groups_query(event.id if event else None).all()
    duplicate_keys = [
        (group[0], group[1], group[2])
        for group in duplicate_groups
        if (not church_id or group[1] == church_id)
    ]

    query = Participant.query
    if event:
        query = query.filter(Participant.event_id == event.id)
    elif event_id:
        query = query.filter(Participant.event_id == event_id)
    if status == "awaiting_review":
        query = query.filter(Participant.status.in_(("pending", "under_review")))
    elif status:
        query = query.filter(Participant.status == status)
    if church_id:
        query = query.filter(Participant.church_id == church_id)
    if search:
        query = query.filter(Participant.full_name.contains(search, autoescape=True))
    if duplicates_only:
        if not duplicate_keys:
            query = query.filter(false())
        else:
            query = query.filter(
                tuple_(
                    Participant.event_id, Participant.church_id, Participant.name_key
                ).in_(duplicate_keys)
            )
    rows = (
        query.order_by(Participant.created_at.desc())
        .limit(501)
        .all()
    )
    duplicate_key_set = set(duplicate_keys)
    histories = {}
    finalized_event_ids = {
        row[0]
        for row in db.session.query(Draw.event_id)
        .filter(Draw.status == "finalized")
        .all()
    }
    if rows:
        history_rows = (
            RegistrationStatusHistory.query.filter(
                RegistrationStatusHistory.participant_id.in_(
                    [person.id for person in rows]
                )
            )
            .order_by(RegistrationStatusHistory.created_at.desc())
            .all()
        )
        for history in history_rows:
            histories.setdefault(history.participant_id, []).append(history)
    return render_template(
        "admin/participants.html",
        event=event,
        events=events,
        churches=churches,
        participants=rows[:500],
        histories=histories,
        duplicate_keys=duplicate_key_set,
        finalized_event_ids=finalized_event_ids,
        filters={
            "event_id": str(event.id) if event else "",
            "church_id": str(church_id) if church_id else "",
            "status": status,
            "q": search,
            "duplicates": duplicates_only,
        },
        truncated=len(rows) > 500,
    )


@bp.get("/duplicates")
@admin_required
def duplicate_report():
    groups = _duplicate_groups_query().all()
    keys = [(group[0], group[1], group[2]) for group in groups]
    rows = []
    if keys:
        rows = (
            Participant.query.filter(
                Participant.status.in_(ACTIVE_PARTICIPANT_STATUSES),
                tuple_(
                    Participant.event_id, Participant.church_id, Participant.name_key
                ).in_(keys),
            )
            .order_by(Participant.event_id, Participant.church_id, Participant.name_key)
            .all()
        )
    return render_template(
        "admin/duplicates.html", duplicate_groups=groups, participants=rows
    )


@bp.post("/participants/<uuid:participant_id>/review")
@admin_required
def review_participant(participant_id):
    participant = db.session.get(Participant, participant_id)
    if not participant:
        abort(404)
    event = db.session.get(Event, participant.event_id)
    target = request.form.get("status")
    if (
        event.status != "verification"
        or participant.status not in {"pending", "under_review"}
        or target not in {"approved", "rejected"}
    ):
        _audit_rejected(
            "participant_review_rejected",
            "participant",
            participant.id,
            {"requested_status": target},
        )
        flash("Only pending submissions can be reviewed during verification.", "danger")
        return redirect(_participant_return_url(event))
    previous_status = participant.status
    participant.status = target
    record_registration_status(participant, previous_status)
    record_audit(
        "participant_reviewed",
        "participant",
        participant.id,
        {"status": target, "event_id": str(event.id)},
    )
    db.session.commit()
    flash("Participant review saved.", "success")
    return redirect(_participant_return_url(event))


@bp.post("/participants/<uuid:participant_id>/correct")
@admin_required
def correct_participant(participant_id):
    participant = db.session.get(Participant, participant_id)
    if not participant:
        abort(404)
    event = db.session.get(Event, participant.event_id)
    if (
        event.status in {"drawing_finalized", "distribution", "completed", "archived"}
        or Draw.query.filter_by(event_id=event.id, status="finalized").first()
        or participant.status == "withdrawn"
    ):
        _audit_rejected(
            "participant_correction_rejected", "participant", participant.id
        )
        flash("Participant details cannot be corrected after the draw is finalized.", "danger")
        return redirect(_participant_return_url(event))
    name = _clean_text(request.form.get("full_name"), 200)
    raw_preference = request.form.get("gift_preference", "")
    preference = _clean_text(raw_preference, 500) or None
    reason = _clean_text(request.form.get("reason"), 2000)
    if not name or not reason or (raw_preference.strip() and preference is None):
        _audit_rejected(
            "participant_correction_rejected",
            "participant",
            participant.id,
            {"reason_code": "invalid_correction_fields"},
        )
        flash("A valid name and correction reason are required.", "danger")
        return redirect(_participant_return_url(event))
    name_key = normalize_name(name)
    duplicate = Participant.query.filter(
        Participant.id != participant.id,
        Participant.event_id == event.id,
        Participant.church_id == participant.church_id,
        Participant.name_key == name_key,
        Participant.status.in_(ACTIVE_PARTICIPANT_STATUSES),
    ).first()
    if duplicate and participant.status in ACTIVE_PARTICIPANT_STATUSES:
        _audit_rejected(
            "participant_correction_rejected",
            "participant",
            participant.id,
            {"reason_code": "duplicate_active_name"},
        )
        flash("Another active submission at this church already uses that name.", "danger")
        return redirect(_participant_return_url(event))
    before = {
        "full_name": participant.full_name,
        "gift_preference": participant.gift_preference,
    }
    participant.full_name = name
    participant.name_key = name_key
    participant.gift_preference = preference
    record_audit(
        "participant_corrected",
        "participant",
        participant.id,
        {
            "before": before,
            "after": {"full_name": name, "gift_preference": preference},
            "reason": reason,
        },
    )
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash("Another active submission already uses that name.", "danger")
        return redirect(_participant_return_url(event))
    flash("Participant details corrected.", "success")
    return redirect(_participant_return_url(event))


@bp.post("/participants/<uuid:participant_id>/credential/reset")
@admin_required
def reset_participant_credential(participant_id):
    participant = db.session.get(Participant, participant_id)
    if not participant:
        abort(404)
    reason = _clean_text(request.form.get("reason"), 2000)
    if participant.status not in ACTIVE_PARTICIPANT_STATUSES:
        _audit_rejected(
            "participant_credential_reset_rejected",
            "participant",
            participant.id,
            {"reason_code": "participant_not_active"},
        )
        flash("Credentials can be reset only for active registrations.", "danger")
        return redirect(_participant_return_url(participant.event))
    if not reason:
        _audit_rejected(
            "participant_credential_reset_rejected",
            "participant",
            participant.id,
            {"reason_code": "missing_reason"},
        )
        flash("A reason is required to reset participant credentials.", "danger")
        return redirect(_participant_return_url(participant.event))

    credential = participant.retrieval_credential
    reference_changed = credential is None
    if credential is None:
        for _ in range(5):
            reference = new_reference()
            if not RetrievalCredential.query.filter_by(reference=reference).first():
                pin = new_pin()
                credential = RetrievalCredential(
                    participant_id=participant.id,
                    reference=reference,
                    pin_hash=hash_pin(pin),
                )
                db.session.add(credential)
                break
        else:
            _audit_rejected(
                "participant_credential_reset_rejected",
                "participant",
                participant.id,
                {"reason_code": "reference_generation_failed"},
            )
            flash("Could not reset credentials; please retry.", "danger")
            return redirect(_participant_return_url(participant.event))
    else:
        pin = new_pin()
        credential.pin_hash = hash_pin(pin)
        credential.revoked_at = None
        credential.expires_at = None
    clear_rate_limit_attempt("lookup-reference", credential.reference)
    record_audit(
        "participant_credential_reset",
        "participant",
        participant.id,
        {"reference_changed": reference_changed, "reason": reason},
    )
    db.session.commit()
    return render_template(
        "admin/credential_reset.html",
        participant=participant,
        credential=credential,
        pin=pin,
    )


@bp.post("/events/<uuid:event_id>/participants/<uuid:participant_id>/withdraw")
@admin_required
def withdraw(event_id, participant_id):
    event = db.session.get(Event, event_id)
    participant = db.session.get(Participant, participant_id)
    if not event or not participant or participant.event_id != event.id:
        abort(404)
    reason = _clean_text(request.form.get("reason"), 2000)
    if not reason:
        _audit_rejected(
            "participant_withdrawal_rejected",
            "participant",
            participant.id,
            {"reason_code": "missing_reason"},
        )
        flash("A withdrawal reason is required.", "danger")
        return redirect(_participant_return_url(event))
    try:
        if event.status == "drawing_finalized":
            credential = participant.retrieval_credential
            if credential:
                credential.revoked_at = db.func.now()
            draw = withdraw_after_draw(event, participant, reason)
            flash(
                f"Participant withdrawn; draw version {draw.version} is now active.",
                "success",
            )
            return redirect(_participant_return_url(event))
        if event.status in {"distribution", "completed", "archived"}:
            raise DrawError("Participants cannot be withdrawn after distribution begins")
        if participant.status not in PARTICIPANT_STATUSES - {"withdrawn"}:
            raise DrawError("Participant is not eligible for withdrawal")
        if participant.status == "approved":
            remaining = Participant.query.filter_by(
                event_id=event.id, status="approved"
            ).count() - 1
            if event.status == "ready_for_drawing" and remaining < 2:
                raise DrawError(
                    "At least two approved participants must remain before drawing"
                )
        previous_status = participant.status
        participant.status = "withdrawn"
        participant.withdrawal_reason = reason
        if participant.retrieval_credential:
            participant.retrieval_credential.revoked_at = db.func.now()
        record_registration_status(participant, previous_status, reason)
        record_audit(
            "participant_withdrawn",
            "participant",
            participant.id,
            {"event_id": str(event.id), "reason": reason},
        )
        db.session.commit()
    except DrawError as exc:
        db.session.rollback()
        _audit_rejected(
            "participant_withdrawal_rejected", "participant", participant.id
        )
        flash(str(exc), "danger")
        return redirect(_participant_return_url(event))
    except Exception:
        db.session.rollback()
        raise
    flash("Participant withdrawn.", "success")
    return redirect(_participant_return_url(event))


@bp.get("/audit")
@admin_required
def audit():
    entries = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(500).all()
    return render_template("admin/audit.html", entries=entries)


@bp.get("/reports")
@admin_required
def reports():
    events = Event.query.order_by(Event.created_at.desc()).all()
    event_id = _report_event_id(required=False)
    report_data = None
    if event_id:
        try:
            report_data = build_event_reports(event_id)
        except ReportError:
            abort(404)
        record_audit("reports_viewed", "event", event_id)
        db.session.commit()
    return render_template(
        "admin/reports.html",
        events=events,
        report_data=report_data,
    )


@bp.get("/reports/<report_name>.csv")
@admin_required
def export_report(report_name):
    allowed = {
        "masterlist",
        "church-summary",
        "drawing-validation",
        "distribution-status",
        "withdrawals",
        "audit",
    }
    if report_name not in allowed:
        abort(404)
    event_id = None if report_name == "audit" else _report_event_id()
    data = None
    if event_id is not None:
        try:
            data = build_event_reports(event_id)
        except ReportError:
            abort(404)
    headers, rows = _report_csv_rows(report_name, data)
    record_audit(
        "report_export_downloaded",
        "report",
        report_name,
        {
            "event_id": str(event_id) if event_id else None,
            "row_count": len(rows),
        },
    )
    db.session.commit()
    output = csv_file(headers, rows)
    suffix = f"-{event_id}" if event_id else ""
    return send_file(
        output,
        as_attachment=True,
        download_name=f"{report_name}{suffix}.csv",
        mimetype="text/csv",
    )


@bp.get("/reports/assignments.csv")
@admin_required
def export_assignments():
    event_id = _report_event_id()
    try:
        event, draw, assignment_rows = build_assignment_rows(event_id)
    except ReportError:
        abort(404)
    rows = [
        (event.name, draw.version, church_name, giver_name, recipient_name)
        for giver_name, church_name, recipient_name in assignment_rows
    ]
    record_audit(
        "assignment_export_downloaded",
        "draw",
        draw.id,
        {
            "event_id": str(event.id),
            "draw_version": draw.version,
            "row_count": len(rows),
        },
    )
    db.session.commit()
    output = csv_file(
        ("event", "draw_version", "giver_church", "giver", "recipient"), rows
    )
    return send_file(
        output,
        as_attachment=True,
        download_name=f"CONFIDENTIAL-assignments-draw-{draw.version}-{event.id}.csv",
        mimetype="text/csv",
    )


def _report_event_id(required=True):
    raw_event_id = request.args.get("event_id", "").strip()
    if not raw_event_id:
        if required:
            abort(400)
        return None
    try:
        return uuid.UUID(raw_event_id)
    except (ValueError, TypeError):
        abort(400)


def _report_csv_rows(report_name, data):
    if report_name == "audit":
        entries = build_audit_rows()
        return (
            ("created_at", "actor_id", "action", "entity_type", "entity_id", "details"),
            [
                (
                    entry.created_at.isoformat(),
                    entry.actor_id,
                    entry.action,
                    entry.entity_type,
                    entry.entity_id,
                    entry.details,
                )
                for entry in entries
            ],
        )
    event = data["event"]
    if report_name == "masterlist":
        return (
            ("event", "church", "participant", "status", "submitted_at"),
            [
                (
                    event.name,
                    church_name,
                    participant.full_name,
                    participant.status,
                    participant.created_at.isoformat(),
                )
                for participant, church_name in data["masterlist"]
            ],
        )
    if report_name == "church-summary":
        return (
            ("church", "total", "approved", "pending_or_under_review", "rejected", "withdrawn"),
            [tuple(row) for row in data["church_summary"]],
        )
    if report_name == "drawing-validation":
        return (
            ("event", "event_status", "draw_version", "passed", "check", "detail"),
            [
                (
                    event.name,
                    event.status,
                    data["validation"]["draw_version"] or "",
                    check["passed"],
                    check["label"],
                    check["detail"],
                )
                for check in data["validation"]["checks"]
            ],
        )
    if report_name == "distribution-status":
        return (
            (
                "event",
                "draw_version",
                "church",
                "packet_version",
                "status",
                "generated_at",
                "print_count",
                "printed_at",
                "distributed_at",
                "acknowledged_at",
                "assignments",
                "acknowledged",
                "pending",
                "replacement_reason",
            ),
            [
                (
                    event.name,
                    draw_version,
                    church_name,
                    batch.version,
                    batch.status,
                    batch.packet_generated_at.isoformat()
                    if batch.packet_generated_at
                    else "",
                    batch.print_count,
                    batch.printed_at.isoformat() if batch.printed_at else "",
                    batch.distributed_at.isoformat() if batch.distributed_at else "",
                    batch.acknowledged_at.isoformat()
                    if batch.acknowledged_at
                    else "",
                    record_count,
                    acknowledged_count,
                    pending_count,
                    batch.replacement_reason or "",
                )
                for (
                    batch,
                    church_name,
                    draw_version,
                    record_count,
                    acknowledged_count,
                    pending_count,
                ) in data["distribution"]
            ],
        )
    if report_name == "withdrawals":
        return (
            ("event", "participant", "church", "withdrawn_at", "reason"),
            [
                (
                    event.name,
                    participant.full_name,
                    church_name,
                    history.created_at.isoformat(),
                    history.reason or "",
                )
                for history, participant, church_name in data["withdrawals"]
            ],
        )
    raise ValueError(f"Unknown report: {report_name}")


def _duplicate_groups_query(event_id=None):
    query = (
        db.session.query(
            Participant.event_id,
            Participant.church_id,
            Participant.name_key,
            func.count(Participant.id).label("duplicate_count"),
        )
        .add_columns(Event.name, Church.name)
        .filter(Participant.status.in_(ACTIVE_PARTICIPANT_STATUSES))
        .join(Event, Participant.event_id == Event.id)
        .join(Church, Participant.church_id == Church.id)
        .group_by(
            Participant.event_id,
            Participant.church_id,
            Participant.name_key,
            Event.name,
            Church.name,
        )
        .having(func.count(Participant.id) > 1)
    )
    if event_id:
        query = query.filter(Participant.event_id == event_id)
    return query


def _audit_rejected(action, entity_type, entity_id=None, details=None):
    record_audit(action, entity_type, entity_id, details)
    db.session.commit()


def _participant_return_url(event):
    destination = request.form.get("return_to", "")
    is_local_participant_path = destination.startswith("/admin/participants") or (
        destination.startswith("/admin/events/")
        and "/participants" in destination
    )
    if (
        is_local_participant_path
        and not destination.startswith("//")
        and "\\" not in destination
    ):
        return destination
    if "/events/" in request.path:
        return url_for("admin.participants", event_id=event.id)
    return url_for("admin.all_participants")


def _distribution_redirect(batch_id):
    batch = db.session.get(DistributionBatch, batch_id)
    if not batch:
        return redirect(url_for("admin.dashboard"))
    draw = db.session.get(Draw, batch.draw_id)
    if not draw:
        return redirect(url_for("admin.dashboard"))
    return redirect(url_for("admin.distribution_control", event_id=draw.event_id))


def _clean_text(value, maximum):
    text = " ".join((value or "").split())
    return text if text and len(text) <= maximum else ""


def _parse_budget(value):
    value = (value or "").strip()
    if not value:
        return None
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("Enter a valid gift budget.") from exc
    if not amount.is_finite() or amount < 0 or amount > Decimal("99999999.99"):
        raise ValueError("Gift budget must be between 0 and 99,999,999.99.")
    if amount.as_tuple().exponent < -2:
        raise ValueError("Gift budget may have at most two decimal places.")
    return amount.quantize(Decimal("0.01"))


def _decimal_text(value):
    return str(value) if value is not None else None
