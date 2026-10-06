"""Conservative deterministic comparison keys; no provider-specific email rewriting."""

import re
from urllib.parse import urlsplit


def normalize(kind: str, value: str) -> str:
    value = value.strip()
    if kind == "email":
        return value.casefold()
    if kind == "phone":
        return re.sub(r"[^0-9+]", "", value)
    if kind == "url":
        parsed = urlsplit(value if "://" in value else "https://" + value)
        return (parsed.hostname or "").casefold().rstrip(".")
    if kind == "name":
        return " ".join(value.casefold().split())
    return value
