import csv
import os
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import current_app

from ..models import (
    AuditLog,
    DistributionBatch,
    DistributionRecord,
    Participant,
    RetrievalCredential,
    User,
    db,
)
from .pins import hash_pin, new_pin, new_reference


class CredentialResetError(ValueError):
    pass


ACTIVE_STATUSES = {"pending", "under_review", "approved"}


def reset_all_credentials(passwords_by_user_id):
    users = User.query.order_by(User.username).all()
    expected_user_ids = {str(user.id) for user in users}
    if set(passwords_by_user_id) != expected_user_ids:
        raise CredentialResetError("A new password is required for every staff account.")

    participants = Participant.query.order_by(Participant.created_at).all()
    credentials = RetrievalCredential.query.all()
    used_references = {credential.reference for credential in credentials}
    now = datetime.now(timezone.utc)
    export_rows = []
    export_path = None

    try:
        for user in users:
            user.set_password(passwords_by_user_id[str(user.id)])
            db.session.add(
                AuditLog(
                    action="staff_password_reset",
                    entity_type="user",
                    entity_id=str(user.id),
                    details={"source": "reset_all_credentials"},
                )
            )

        for participant in participants:
            credential = participant.retrieval_credential
            if participant.status not in ACTIVE_STATUSES and credential is None:
                continue
            reference = _new_unique_reference(used_references)
            pin = new_pin()
            if credential is None:
                credential = RetrievalCredential(
                    participant_id=participant.id,
                    reference=reference,
                    pin_hash=hash_pin(pin),
                )
                db.session.add(credential)
            else:
                credential.reference = reference
                credential.pin_hash = hash_pin(pin)
                credential.last_used_at = None
                if participant.status in ACTIVE_STATUSES:
                    credential.revoked_at = None
                    credential.expires_at = None
                else:
                    credential.revoked_at = credential.revoked_at or now
            db.session.add(
                AuditLog(
                    action="participant_credential_reset",
                    entity_type="participant",
                    entity_id=str(participant.id),
                    details={
                        "source": "reset_all_credentials",
                        "reference_changed": True,
                    },
                )
            )
            if participant.status in ACTIVE_STATUSES:
                export_rows.append(
                    (
                        participant.event.name,
                        participant.church.name,
                        participant.full_name,
                        reference,
                        pin,
                    )
                )

        batches = DistributionBatch.query.all()
        for batch in batches:
            batch.status = "void"
            batch.packet_path = None
            batch.packet_generated_at = None
            for record in DistributionRecord.query.filter_by(batch_id=batch.id):
                record.card_path = None
            db.session.add(
                AuditLog(
                    action="distribution_packet_invalidated",
                    entity_type="distribution_batch",
                    entity_id=str(batch.id),
                    details={"reason": "all_credentials_reset"},
                )
            )

        export_path = _write_credential_export(export_rows)
        db.session.add(
            AuditLog(
                action="all_credentials_reset",
                entity_type="credentials",
                details={
                    "staff_count": len(users),
                    "participant_count": len(export_rows),
                    "invalidated_packet_count": len(batches),
                    "export_file": export_path.name,
                },
            )
        )
        db.session.commit()
        return export_path
    except Exception:
        db.session.rollback()
        if export_path and export_path.exists():
            export_path.unlink()
        raise


def _new_unique_reference(used_references):
    for _ in range(100):
        reference = new_reference()
        if reference not in used_references:
            used_references.add(reference)
            return reference
    raise CredentialResetError("Could not generate unique participant references.")


def _write_credential_export(rows):
    configured = current_app.config.get("PRIVATE_STORAGE_DIR")
    root = Path(configured) if configured else Path(current_app.instance_path) / "private"
    if not root.is_absolute():
        root = Path(current_app.instance_path) / root
    root = root.resolve()
    static_root = Path(current_app.static_folder).resolve()
    if root == static_root or static_root in root.parents:
        raise CredentialResetError("Credential exports must be stored outside static files.")

    directory = root / "credential-reset"
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    filename = f"participants-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}.csv"
    target = directory / filename
    temporary = directory / f".{filename}.{secrets.token_hex(8)}.tmp"
    descriptor = os.open(str(temporary), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8-sig", newline="") as output:
            writer = csv.writer(output)
            writer.writerow(("event", "church", "participant", "reference", "pin"))
            writer.writerows(rows)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise
    return target
