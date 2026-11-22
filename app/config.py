import os


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY")
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", "sqlite:///instance/bunotan.db"
    )
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
