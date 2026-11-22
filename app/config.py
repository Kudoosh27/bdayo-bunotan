import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def database_settings():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        app_env = os.environ.get("APP_ENV", "").casefold()
        if app_env not in {"", "development", "dev", "local", "testing"}:
            raise RuntimeError("DATABASE_URL must be configured outside local development")
        return "sqlite:///instance/bunotan.db", {}

    if database_url.startswith("postgres://"):
        database_url = "postgresql+psycopg://" + database_url[len("postgres://") :]
    elif database_url.startswith("postgresql://"):
        database_url = "postgresql+psycopg://" + database_url[len("postgresql://") :]

    parsed = urlsplit(database_url)
    if parsed.scheme == "postgresql+psycopg":
        query = [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key != "channel_binding"
        ]
        query = [(key, value) for key, value in query if key != "sslmode"]
        query.append(("sslmode", "require"))
        database_url = urlunsplit(parsed._replace(query=urlencode(query)))
        return database_url, {"pool_pre_ping": True, "pool_recycle": 280}
    return database_url, {}


DATABASE_URI, ENGINE_OPTIONS = database_settings()


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY")
    SQLALCHEMY_DATABASE_URI = DATABASE_URI
    SQLALCHEMY_ENGINE_OPTIONS = ENGINE_OPTIONS
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SESSION_COOKIE_SECURE = (
        os.environ.get("SESSION_COOKIE_SECURE") == "1"
        or os.environ.get("APP_ENV", "").casefold() == "production"
    )
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    MAX_CONTENT_LENGTH = 2 * 1024 * 1024
    LOOKUP_MAX_ATTEMPTS = int(os.environ.get("LOOKUP_MAX_ATTEMPTS", "5"))
    LOOKUP_LOCK_MINUTES = int(os.environ.get("LOOKUP_LOCK_MINUTES", "15"))
    PRIVACY_NOTICE_VERSION = os.environ.get("PRIVACY_NOTICE_VERSION", "1")
    PUBLIC_CONTACT_EMAIL = os.environ.get("PUBLIC_CONTACT_EMAIL")
    PRIVATE_STORAGE_DIR = os.environ.get("PRIVATE_STORAGE_DIR")
