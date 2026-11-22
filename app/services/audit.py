from flask import has_request_context

from ..auth_state import current_user
from ..models import AuditLog, db


def record_audit(action, entity_type, entity_id=None, details=None):
    actor_id = (
        current_user.id
        if has_request_context() and current_user.is_authenticated
        else None
    )
    db.session.add(
        AuditLog(
            actor_id=actor_id,
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id) if entity_id is not None else None,
            details=details or {},
        )
    )
