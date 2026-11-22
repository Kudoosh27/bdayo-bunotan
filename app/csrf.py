import hmac
import secrets

from flask import abort, request, session


def csrf_token():
    token = session.get("csrf_token")
    if not isinstance(token, str) or not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def validate_csrf_request():
    if request.method != "POST":
        return
    expected = session.get("csrf_token")
    supplied = request.form.get("csrf_token")
    if (
        not isinstance(expected, str)
        or not isinstance(supplied, str)
        or not hmac.compare_digest(expected, supplied)
    ):
        abort(400)
