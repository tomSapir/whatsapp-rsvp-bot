"""Phone-number validation and canonicalization to E.164 (PLAN §7).

``invitations.phone`` is the UNIQUE natural key the webhook matches inbound senders against,
so every number — whether typed by the Host in the Streamlit app or arriving as a webhook
``wa_id`` — must collapse to the same canonical E.164 form (e.g. ``+972541234567``) before it
is stored or compared. Israel (``IL``) is the default region for nationally formatted input
(``054-123-4567``); an explicit ``+<country code>`` overrides it; bare ``wa_id`` digits
(``972541234567``, no ``+``) are treated as international. Anything that isn't a valid number
is rejected — never stored.
"""

from __future__ import annotations

import re

import phonenumbers

DEFAULT_REGION = "IL"


class InvalidPhoneNumber(ValueError):
    """Raised when input cannot be parsed and validated as a real phone number."""

    def __init__(self, raw: object) -> None:
        super().__init__(f"not a valid phone number: {raw!r}")
        self.raw = raw


def normalize_phone(raw: str, default_region: str = DEFAULT_REGION) -> str:
    """Return ``raw`` canonicalized to E.164, or raise :class:`InvalidPhoneNumber`.

    Resolution order for input *without* a leading ``+``:

    1. parse as a national number in ``default_region`` — handles local entry like
       ``054-123-4567``;
    2. failing that, prepend ``+`` and parse as international — handles a bare ``wa_id``
       such as ``972541234567``.

    A leading ``+`` is always honored as-is (its country code overrides the region). The
    first interpretation that is a *valid* number wins; if none validates, the input is
    rejected.
    """
    if not raw or not raw.strip():
        raise InvalidPhoneNumber(raw)

    candidate = raw.strip()
    if candidate.startswith("+"):
        attempts: list[tuple[str, str | None]] = [(candidate, None)]
    else:
        digits = re.sub(r"\D", "", candidate)
        attempts = [(candidate, default_region), ("+" + digits, None)]

    for number_str, region in attempts:
        try:
            parsed = phonenumbers.parse(number_str, region)
        except phonenumbers.NumberParseException:
            continue
        if phonenumbers.is_valid_number(parsed):
            return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)

    raise InvalidPhoneNumber(raw)
