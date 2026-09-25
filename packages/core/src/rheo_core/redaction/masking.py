"""Free-text masking: contact values and secret references inside a string.

The field tiers stop a ``restricted`` *field*; this stops a restricted *value* that
sits inside a field the tiers allow, such as an email address in a message body
(``runtime-and-mcp.md`` § The context builder). It runs over every string a model is
sent, from the context builder and from the tool facade alike, after tiering, and over
a run's task text.

**Conservative on purpose.** A mask that fires on a timestamp, a size list or a record
reference corrupts the text a model reasons over, and every tool result is full of
them, so the patterns match only unmistakable shapes and the tests pin the non-matches
as hard as the matches:

- **Email**: ``local@domain.tld``, Unicode letters allowed in both halves
  (``josé@example.com``, ``user@münchen.de``), a letters-only TLD of two or more.
- **North American phone**: ``NPA-NXX-XXXX`` with the NANP's own rule that the area
  code and the exchange each start 2-9, one separator used throughout (``-``, ``.``, a
  space, or an en or figure dash), optionally ``(NPA)``, optionally led by ``1`` or
  ``+1`` and a separator, or ``+1`` and ten bare digits. Never inside a longer run of
  digits, separators or dots, which is what keeps ``192.168.100.1000`` and
  ``100-200-3000`` out.
- **International phone**: ``+`` and a country code starting 2-9 (``+1`` is the NANP
  form above, so ``+100 200 300`` is neither), then 9 to 15 digits in all.
- **Secret reference**: ``secret://<backend>/<id>``, any case, the id running to the
  next space or quote (``%`` escapes included), less trailing sentence punctuation.
  Masked always, whatever the allowance says.

**What is left, and knowingly.** Three space-separated numbers that happen to satisfy
the NANP digit rule (``sizes 256 512 1024``) still mask as a phone; dropping the space
separator would miss ``250 555 0100``, which is how people write numbers. A
seven-digit local number (``555-0100``) is not masked: it is a bare ``3-4`` group, the
shape of many codes and ranges. An exchange starting 0 or 1 (``555-010-0123``) is not a
valid North American number and is not masked. ``git@github.com`` in an SSH remote is
masked as an email. Postal addresses and handles are not recognised at all; that is what
the field tiers are for, and this is the backstop behind them, not a substitute.
"""

import re
import unicodedata
from collections.abc import Iterator
from typing import Final

from rheo_core.redaction.policy import TierPolicy

EMAIL_MASK: Final = "[email withheld]"
PHONE_MASK: Final = "[phone withheld]"
SECRET_MASK: Final = "[secret reference withheld]"
MASK_TOKENS: Final = (EMAIL_MASK, PHONE_MASK, SECRET_MASK)
"""Every token this module writes. The tool facade refuses a write carrying one, so a
model that was shown masked text can never write the mask back over the real value."""

_EMAIL: Final = re.compile(
    r"(?<![\w.%+-])[\w.%+-]+@(?:[^\W_](?:[\w-]*[^\W_])?\.)+[^\W\d_]{2,}(?![\w-])"
)
_DASHES: Final = "\u2011\u2012\u2013"
"""Non-breaking hyphen, figure dash, en dash: what word processors put in numbers."""
_SEP: Final = f"[-. {_DASHES}]"
_EXTENSION: Final = r"(?:\s?(?:x|ext\.?|#)\s?\d{1,6})?"
_NORTH_AMERICAN: Final = re.compile(
    # Not after a word character, a sign or a dash, and not after "<digit>." (the
    # tail of a dotted quad); a letter then a dot ("Tel.250-...") is fine.
    rf"(?<![\w+\-{_DASHES}])(?<!\d\.)"
    rf"(?:"
    rf"\+1[2-9]\d{{2}}[2-9]\d{{6}}"
    rf"|(?:\+?1{_SEP})?\([2-9]\d{{2}}\) ?[2-9]\d{{2}}{_SEP}\d{{4}}"
    rf"|(?:\+?1(?P<lead>{_SEP}))?[2-9]\d{{2}}(?P<sep>{_SEP})[2-9]\d{{2}}(?P=sep)\d{{4}}"
    rf"){_EXTENSION}"
    rf"(?![\w]|[-.{_DASHES}]\d)",
    re.IGNORECASE,
)
_INTERNATIONAL: Final = re.compile(
    rf"(?<![\w+])\+[2-9]\d{{0,2}}(?:[ .\-{_DASHES}]?\(?\d{{1,4}}\)?){{2,6}}(?![\w])"
)
_SECRET_REFERENCE: Final = re.compile(
    r"secret://[A-Za-z0-9_-]+/[^\s\"'<>]*[^\s\"'<>.,;:!?)\]]", re.IGNORECASE
)
_MIN_INTERNATIONAL_DIGITS: Final = 9
_MAX_INTERNATIONAL_DIGITS: Final = 15


def _international(match: re.Match[str]) -> str:
    digits = sum(character.isdigit() for character in match.group(0))
    if _MIN_INTERNATIONAL_DIGITS <= digits <= _MAX_INTERNATIONAL_DIGITS:
        return PHONE_MASK
    return match.group(0)


def mask_secret_references(text: str) -> str:
    """``text`` with every secret reference replaced, unconditionally."""
    return _SECRET_REFERENCE.sub(SECRET_MASK, text)


def mask_contact_values(text: str) -> str:
    """``text`` with every email address and phone number replaced."""
    masked = _EMAIL.sub(EMAIL_MASK, text)
    masked = _NORTH_AMERICAN.sub(PHONE_MASK, masked)
    return _INTERNATIONAL.sub(_international, masked)


def has_contact_value(text: str) -> bool:
    return mask_contact_values(text) != text


def mask_for_model(text: str, policy: TierPolicy) -> str:
    """``text`` as it may be sent to a model under ``policy``.

    Secret references are always masked. Contact values are masked unless the policy
    allows contact points (``redaction.contact_points_to_model``, both floors true);
    the allowance is read only when a contact value is actually present, so a string
    with none never costs a settings read.
    """
    masked = mask_secret_references(text)
    if not has_contact_value(masked):
        return masked
    if policy.contact_points_allowed:
        return masked
    return mask_contact_values(masked)


_WHITESPACE: Final = re.compile(r"\s+")


def _normal(text: str) -> str:
    """``text`` compatibility-normalised, casefolded, whitespace collapsed.

    NFKC folds full-width brackets and letters and turns a no-break space into a
    space, casefolding catches ``[EMAIL WITHHELD]``, and collapsing whitespace catches
    a doubled or tab-separated token. What a model might retype or a client might
    transcode, the refusal still recognises.
    """
    folded = unicodedata.normalize("NFKC", text).casefold()
    return _WHITESPACE.sub(" ", folded)


_NORMAL_TOKENS: Final = tuple((token, _normal(token)) for token in MASK_TOKENS)


def mask_tokens_in(value: object) -> Iterator[str]:
    """Every mask token found in any string inside ``value`` (mappings, sequences and
    sets walked, keys included), matched after :func:`_normal`, and reported in its
    canonical spelling."""
    if isinstance(value, str):
        text = _normal(value)
        yield from (token for token, normal in _NORMAL_TOKENS if normal in text)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from mask_tokens_in(key)
            yield from mask_tokens_in(item)
    elif isinstance(value, list | tuple | set | frozenset):
        for item in value:
            yield from mask_tokens_in(item)
