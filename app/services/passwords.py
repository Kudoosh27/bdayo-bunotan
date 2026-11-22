from werkzeug.security import check_password_hash, generate_password_hash


def hash_password(password):
    return generate_password_hash(password, method="scrypt")


def verify_password(password_hash, password):
    try:
        return check_password_hash(password_hash, password)
    except (TypeError, ValueError):
        return False
