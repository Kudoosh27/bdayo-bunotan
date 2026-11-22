import re
import unicodedata


_TRUE_VALUES = {"yes", "true", "1"}
_FALSE_VALUES = {"no", "false", "0"}


def validate_participant_name(value, maximum=200):
    name = " ".join(str(value or "").split())
    if not name or len(name) > maximum or not re.search(r"\w", name, re.UNICODE):
        return None
    return name


def parse_boolean(value):
    normalized = str(value if value is not None else "").strip().casefold()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    return None


def clean_optional_text(value):
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split()) or None
