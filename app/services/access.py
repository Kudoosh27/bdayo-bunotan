from functools import wraps
import uuid

from flask import abort, flash, redirect, url_for

from ..auth_state import current_user


def roles_required(*roles):
    def decorate(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated:
                flash("Please sign in to continue.", "warning")
                return redirect(url_for("auth.login"))
            if current_user.role not in roles:
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorate


def admin_required(view):
    return roles_required("admin")(view)


def leader_required(view):
    return roles_required("leader")(view)


def can_access_church(church_id, user=None):
    user = user if user is not None else current_user
    if not getattr(user, "is_authenticated", False):
        return False
    if user.role == "admin":
        return True
    if user.role != "leader" or user.church_id is None:
        return False
    try:
        return uuid.UUID(str(user.church_id)) == uuid.UUID(str(church_id))
    except (ValueError, TypeError, AttributeError):
        return False


def require_church_scope(church_id, user=None):
    if not can_access_church(church_id, user):
        abort(403)
