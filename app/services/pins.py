import secrets
from werkzeug.security import check_password_hash, generate_password_hash

_ALPHABET = "23456789"  # no ambiguous digits


def new_pin(length=6):
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


def new_reference():
    return "SB-" + secrets.token_hex(4).upper()


def hash_pin(pin):
    return generate_password_hash(pin, method="scrypt")


def verify_pin(pin_hash, pin):
    try:
        return check_password_hash(pin_hash, pin)
    except (TypeError, ValueError):
        return False


