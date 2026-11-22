import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import abort, current_app, request
from sqlalchemy import case, delete, select
from sqlalchemy.dialects.sqlite import insert

from ..models import RateLimitAttempt, db


def rate_limit_key(namespace, identity):
    message = f"{namespace}:{identity or ''}".encode("utf-8")
    return hmac.new(
        current_app.config["SECRET_KEY"].encode("utf-8"),
        message,
        hashlib.sha256,
    ).hexdigest()


def rate_limit(namespace, maximum, window_seconds, lock_seconds, methods=None):
    def decorate(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if methods is not None and request.method not in methods:
                return view(*args, **kwargs)
            if not consume_rate_limit(
                namespace,
                request.remote_addr or "",
                maximum,
                window_seconds,
                lock_seconds,
            ):
                abort(429)
            return view(*args, **kwargs)

        return wrapped

    return decorate


def consume_rate_limit(namespace, identity, maximum, window_seconds, lock_seconds):
    key = rate_limit_key(namespace, identity)
    now = datetime.now(timezone.utc)
    expired_window = RateLimitAttempt.window_start <= now - timedelta(
        seconds=window_seconds
    )
    existing_lock = RateLimitAttempt.locked_until > now
    at_limit = RateLimitAttempt.count >= maximum
    next_count = case(
        (existing_lock, RateLimitAttempt.count),
        (expired_window, 1),
        (at_limit, RateLimitAttempt.count),
        else_=RateLimitAttempt.count + 1,
    )
    next_lock = case(
        (existing_lock, RateLimitAttempt.locked_until),
        (expired_window, None),
        (at_limit, now + timedelta(seconds=lock_seconds)),
        else_=None,
    )
    statement = (
        insert(RateLimitAttempt)
        .values(key=key, window_start=now, count=1)
        .on_conflict_do_update(
            index_elements=[RateLimitAttempt.key],
            set_={
                "window_start": case(
                    (expired_window, now), else_=RateLimitAttempt.window_start
                ),
                "count": next_count,
                "locked_until": next_lock,
            },
        )
        .returning(RateLimitAttempt.locked_until)
    )
    locked_until = db.session.execute(statement).scalar_one()
    db.session.commit()
    return not _is_future(locked_until, now)


def is_rate_limit_locked(namespace, identity):
    attempt = db.session.get(RateLimitAttempt, rate_limit_key(namespace, identity))
    return bool(attempt and _is_future(attempt.locked_until, datetime.now(timezone.utc)))


def record_rate_limit_failure(
    namespace, identity, maximum, window_seconds, lock_seconds
):
    key = rate_limit_key(namespace, identity)
    now = datetime.now(timezone.utc)
    expired_window = RateLimitAttempt.window_start <= now - timedelta(
        seconds=window_seconds
    )
    existing_lock = RateLimitAttempt.locked_until > now
    next_count = case(
        (existing_lock, RateLimitAttempt.count),
        (expired_window, 1),
        else_=RateLimitAttempt.count + 1,
    )
    next_lock = case(
        (existing_lock, RateLimitAttempt.locked_until),
        (expired_window, now + timedelta(seconds=lock_seconds) if maximum <= 1 else None),
        (
            RateLimitAttempt.count + 1 >= maximum,
            now + timedelta(seconds=lock_seconds),
        ),
        else_=None,
    )
    statement = (
        insert(RateLimitAttempt)
        .values(
            key=key,
            window_start=now,
            count=1,
            locked_until=now + timedelta(seconds=lock_seconds)
            if maximum <= 1
            else None,
        )
        .on_conflict_do_update(
            index_elements=[RateLimitAttempt.key],
            set_={
                "window_start": case(
                    (expired_window, now), else_=RateLimitAttempt.window_start
                ),
                "count": next_count,
                "locked_until": next_lock,
            },
        )
        .returning(RateLimitAttempt.locked_until)
    )
    locked_until = db.session.execute(statement).scalar_one()
    db.session.commit()
    return _is_future(locked_until, now)


def clear_rate_limit_attempt(namespace, identity):
    db.session.execute(
        delete(RateLimitAttempt).where(
            RateLimitAttempt.key == rate_limit_key(namespace, identity)
        )
    )


def _is_future(value, now):
    if value is None:
        return False
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value > now
