import uuid
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, Index, UniqueConstraint
from flask_sqlalchemy import SQLAlchemy

from .services.passwords import hash_password, verify_password

db = SQLAlchemy()


def _id():
    return db.Column(db.Uuid, primary_key=True, default=uuid.uuid4)


def _now():
    return datetime.now(timezone.utc)


class SchemaVersion(db.Model):
    __tablename__ = "schema_version"
    id = db.Column(db.Integer, primary_key=True, default=1)
    version = db.Column(db.Integer, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=_now, nullable=False)
    __table_args__ = (CheckConstraint("id = 1", name="ck_schema_version_singleton"),)


class Event(db.Model):
    __tablename__ = "events"
    id = _id()
    name = db.Column(db.String(200), nullable=False)
    status = db.Column(db.String(30), nullable=False, default="draft")
    gift_budget = db.Column(db.Numeric(10, 2))
    created_at = db.Column(db.DateTime(timezone=True), default=_now, nullable=False)
    __table_args__ = (
        CheckConstraint(
            "status in ('draft','registration_open','registration_closed',"
            "'verification','ready_for_drawing','drawing_finalized',"
            "'distribution','completed','archived')",
            name="ck_event_status",
        ),
    )


class Church(db.Model):
    __tablename__ = "churches"
    id = _id()
    name = db.Column(db.String(200), nullable=False, unique=True)
    active = db.Column(db.Boolean, default=True, nullable=False)


class Participant(db.Model):
    __tablename__ = "participants"
    id = _id()
    event_id = db.Column(db.Uuid, db.ForeignKey("events.id"), nullable=False)
    church_id = db.Column(db.Uuid, db.ForeignKey("churches.id"), nullable=False)
    event = db.relationship("Event")
    church = db.relationship("Church")
    retrieval_credential = db.relationship(
        "RetrievalCredential", uselist=False, back_populates="participant"
    )
    full_name = db.Column(db.String(200), nullable=False)
    name_key = db.Column(db.String(200), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="pending")
    participation_confirmed = db.Column(db.Boolean, nullable=False, default=False)
    gift_preference = db.Column(db.String(500))
    guardian_consent = db.Column(db.Boolean, nullable=False, default=False)
    is_minor = db.Column(db.Boolean, nullable=False, default=False)
    privacy_consent = db.Column(db.Boolean, nullable=False, default=True)
    privacy_notice_version = db.Column(db.String(30), nullable=False, default="1")
    withdrawal_reason = db.Column(db.Text)
    created_at = db.Column(db.DateTime(timezone=True), default=_now, nullable=False)
    __table_args__ = (
        Index(
            "uq_active_participant",
            "event_id",
            "church_id",
            "name_key",
            unique=True,
            sqlite_where=db.text("status in ('pending','under_review','approved')"),
        ),
        CheckConstraint(
            "status in ('pending','under_review','approved','rejected','withdrawn')",
            name="ck_participant_status",
        ),
    )


class RegistrationStatusHistory(db.Model):
    __tablename__ = "registration_status_history"
    id = _id()
    participant_id = db.Column(
        db.Uuid, db.ForeignKey("participants.id"), nullable=False, index=True
    )
    previous_status = db.Column(db.String(20))
    new_status = db.Column(db.String(20), nullable=False)
    changed_by_id = db.Column(db.Uuid, db.ForeignKey("users.id"))
    reason = db.Column(db.Text)
    created_at = db.Column(db.DateTime(timezone=True), default=_now, nullable=False)
    participant = db.relationship("Participant")
    changed_by = db.relationship("User")
    __table_args__ = (
        CheckConstraint(
            "new_status in ('pending','under_review','approved','rejected','withdrawn')",
            name="ck_registration_history_new_status",
        ),
        CheckConstraint(
            "previous_status is null or previous_status in "
            "('pending','under_review','approved','rejected','withdrawn')",
            name="ck_registration_history_previous_status",
        ),
        CheckConstraint(
            "previous_status is null or previous_status <> new_status",
            name="ck_registration_history_status_changed",
        ),
        Index("ix_registration_history_created_at", "created_at"),
    )


class RetrievalCredential(db.Model):
    __tablename__ = "retrieval_credentials"
    participant_id = db.Column(
        db.Uuid, db.ForeignKey("participants.id"), primary_key=True
    )
    reference = db.Column(db.String(20), nullable=False, unique=True)
    pin_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=_now, nullable=False)
    expires_at = db.Column(db.DateTime(timezone=True))
    last_used_at = db.Column(db.DateTime(timezone=True))
    revoked_at = db.Column(db.DateTime(timezone=True))
    participant = db.relationship("Participant", back_populates="retrieval_credential")
    __table_args__ = (
        Index("ix_retrieval_credentials_expires_at", "expires_at"),
    )


class Draw(db.Model):
    __tablename__ = "draws"
    id = _id()
    event_id = db.Column(db.Uuid, db.ForeignKey("events.id"), nullable=False)
    version = db.Column(db.Integer, nullable=False)
    participant_count = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(20), nullable=False, default="finalized")
    validation_ok = db.Column(db.Boolean, nullable=False)
    cancel_reason = db.Column(db.Text)
    created_at = db.Column(db.DateTime(timezone=True), default=_now, nullable=False)
    __table_args__ = (
        UniqueConstraint("event_id", "version"),
        CheckConstraint("status in ('finalized','superseded')", name="ck_draw_status"),
    )


class Assignment(db.Model):
    __tablename__ = "assignments"
    id = _id()
    draw_id = db.Column(db.Uuid, db.ForeignKey("draws.id"), nullable=False)
    giver_id = db.Column(db.Uuid, db.ForeignKey("participants.id"), nullable=False)
    recipient_id = db.Column(db.Uuid, db.ForeignKey("participants.id"), nullable=False)
    __table_args__ = (
        UniqueConstraint("draw_id", "giver_id"),
        UniqueConstraint("draw_id", "recipient_id"),
        CheckConstraint("giver_id <> recipient_id", name="ck_no_self_assignment"),
    )


class User(db.Model):
    __tablename__ = "users"
    id = _id()
    username = db.Column(db.String(120), nullable=False, unique=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(30), nullable=False)
    church_id = db.Column(db.Uuid, db.ForeignKey("churches.id"))
    church = db.relationship("Church")
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), default=_now, nullable=False)

    __table_args__ = (
        CheckConstraint("role in ('admin','leader')", name="ck_user_role"),
        CheckConstraint(
            "(role = 'leader' and church_id is not null) or "
            "(role = 'admin' and church_id is null)",
            name="ck_user_church_scope",
        ),
        Index("ix_users_church_id", "church_id"),
        Index("ix_users_created_at", "created_at"),
    )

    def set_password(self, password):
        self.password_hash = hash_password(password)

    def check_password(self, password):
        return verify_password(self.password_hash, password)

    def get_id(self):
        return str(self.id)

    @property
    def is_authenticated(self):
        return True

    @property
    def is_anonymous(self):
        return False


class AuditLog(db.Model):
    __tablename__ = "audit_logs"
    id = _id()
    actor_id = db.Column(db.Uuid, db.ForeignKey("users.id"))
    action = db.Column(db.String(100), nullable=False)
    entity_type = db.Column(db.String(80), nullable=False)
    entity_id = db.Column(db.String(36))
    details = db.Column(db.JSON, nullable=False, default=dict)
    created_at = db.Column(db.DateTime(timezone=True), default=_now, nullable=False)
    __table_args__ = (
        Index("ix_audit_logs_actor_created", "actor_id", "created_at"),
        Index("ix_audit_logs_entity", "entity_type", "entity_id"),
    )


class AssignmentAccessLog(db.Model):
    __tablename__ = "assignment_access_logs"
    id = _id()
    assignment_id = db.Column(
        db.Uuid, db.ForeignKey("assignments.id"), nullable=False, index=True
    )
    participant_id = db.Column(
        db.Uuid, db.ForeignKey("participants.id"), nullable=False, index=True
    )
    accessed_by_id = db.Column(db.Uuid, db.ForeignKey("users.id"))
    access_method = db.Column(db.String(30), nullable=False, default="participant_lookup")
    accessed_at = db.Column(db.DateTime(timezone=True), default=_now, nullable=False)
    assignment = db.relationship("Assignment")
    participant = db.relationship("Participant")
    accessed_by = db.relationship("User")
    __table_args__ = (
        CheckConstraint(
            "access_method in ('participant_lookup','admin_review','distribution')",
            name="ck_assignment_access_method",
        ),
        Index("ix_assignment_access_created", "accessed_at"),
    )


class DistributionBatch(db.Model):
    __tablename__ = "distribution_batches"
    id = _id()
    draw_id = db.Column(db.Uuid, db.ForeignKey("draws.id"), nullable=False)
    church_id = db.Column(db.Uuid, db.ForeignKey("churches.id"), nullable=False)
    created_by_id = db.Column(db.Uuid, db.ForeignKey("users.id"), nullable=False)
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    replacement_of_id = db.Column(db.Uuid, db.ForeignKey("distribution_batches.id"))
    replacement_reason = db.Column(db.Text)
    status = db.Column(db.String(20), nullable=False, default="prepared")
    created_at = db.Column(db.DateTime(timezone=True), default=_now, nullable=False)
    packet_path = db.Column(db.String(500))
    packet_generated_at = db.Column(db.DateTime(timezone=True))
    printed_at = db.Column(db.DateTime(timezone=True))
    print_count = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    distributed_at = db.Column(db.DateTime(timezone=True))
    acknowledged_at = db.Column(db.DateTime(timezone=True))
    draw = db.relationship("Draw")
    church = db.relationship("Church")
    created_by = db.relationship("User")
    replacement_of = db.relationship(
        "DistributionBatch",
        remote_side="DistributionBatch.id",
        foreign_keys=[replacement_of_id],
        backref="replacements",
    )
    __table_args__ = (
        UniqueConstraint(
            "draw_id", "church_id", "version", name="uq_distribution_draw_church_version"
        ),
        CheckConstraint(
            "status in ('prepared','distributed','void')",
            name="ck_distribution_batch_status",
        ),
        CheckConstraint(
            "(status = 'distributed' and distributed_at is not null) or "
            "(status = 'prepared' and distributed_at is null) or status = 'void'",
            name="ck_distribution_batch_timestamp",
        ),
        CheckConstraint("version > 0", name="ck_distribution_batch_version"),
        CheckConstraint("print_count >= 0", name="ck_distribution_batch_print_count"),
        CheckConstraint(
            "(packet_path is null and packet_generated_at is null) or "
            "(packet_path is not null and packet_generated_at is not null)",
            name="ck_distribution_batch_packet_file",
        ),
        CheckConstraint(
            "acknowledged_at is null or status in ('distributed','void')",
            name="ck_distribution_batch_acknowledgment",
        ),
        Index("ix_distribution_batches_status", "status"),
        Index("ix_distribution_batches_replacement_of", "replacement_of_id"),
    )


class DistributionRecord(db.Model):
    __tablename__ = "distribution_records"
    id = _id()
    batch_id = db.Column(
        db.Uuid, db.ForeignKey("distribution_batches.id"), nullable=False
    )
    assignment_id = db.Column(
        db.Uuid, db.ForeignKey("assignments.id"), nullable=False
    )
    status = db.Column(db.String(20), nullable=False, default="pending")
    created_at = db.Column(db.DateTime(timezone=True), default=_now, nullable=False)
    delivered_at = db.Column(db.DateTime(timezone=True))
    acknowledged_at = db.Column(db.DateTime(timezone=True))
    card_path = db.Column(db.String(500))
    batch = db.relationship("DistributionBatch", backref="records")
    assignment = db.relationship("Assignment")
    __table_args__ = (
        UniqueConstraint("batch_id", "assignment_id", name="uq_distribution_record"),
        CheckConstraint(
            "status in ('pending','delivered','returned')",
            name="ck_distribution_record_status",
        ),
        CheckConstraint(
            "(status = 'delivered' and delivered_at is not null) or "
            "(status <> 'delivered' and delivered_at is null)",
            name="ck_distribution_record_timestamp",
        ),
        CheckConstraint(
            "acknowledged_at is null or status = 'delivered'",
            name="ck_distribution_record_acknowledgment",
        ),
        Index("ix_distribution_records_status", "status"),
    )


class RateLimitAttempt(db.Model):
    __tablename__ = "rate_limit_attempts"
    key = db.Column(db.String(64), primary_key=True)
    window_start = db.Column(db.DateTime(timezone=True), nullable=False)
    count = db.Column(db.Integer, nullable=False, default=0)
    locked_until = db.Column(db.DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("count >= 0", name="ck_rate_limit_count"),
        Index("ix_rate_limit_window_start", "window_start"),
        Index("ix_rate_limit_locked_until", "locked_until"),
    )
