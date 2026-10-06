"""Conservative deterministic comparison keys; no provider-specific email rewriting."""

import re
from urllib.parse import urlsplit

from rheo_core.operations.refusals import OperationRefused


def normalize(kind: str, value: str) -> str:
    value = value.strip()
    if kind == "email":
        return value.casefold()
    if kind == "phone":
        return re.sub(r"[^0-9+]", "", value)
    if kind == "url":
        try:
            parsed = urlsplit(value if "://" in value else "https://" + value)
            host = (parsed.hostname or "").casefold().rstrip(".")
        except ValueError:
            raise OperationRefused("input_invalid", "invalid domain") from None
        if not host:
            raise OperationRefused("input_invalid", "invalid domain")
        return host
    if kind == "name":
        return " ".join(value.casefold().split())
    return value
