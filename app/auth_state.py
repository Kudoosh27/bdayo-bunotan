from flask import g, has_request_context
from werkzeug.local import LocalProxy


class AnonymousUser:
    is_authenticated = False
    is_anonymous = True
    id = None
    role = None
    church_id = None


_anonymous_user = AnonymousUser()


def _current_user():
    if not has_request_context():
        return _anonymous_user
    return getattr(g, "current_user", _anonymous_user)


current_user = LocalProxy(_current_user)
