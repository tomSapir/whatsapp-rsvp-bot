"""Tests for E.164 phone canonicalization (PLAN §7 · M2).

Covers the four cases called out in STEPS — local IL, explicit non-IL ``+`` country code,
bare ``wa_id``, invalid input — plus the key property the webhook relies on: a Host's local
entry and the corresponding ``wa_id`` must collapse to the *same* canonical key.
"""

import pytest

from app.phone import InvalidPhoneNumber, normalize_phone


# Numbers must be real per libphonenumber's metadata: +972502345678 (050-234-5678) is a
# valid IL mobile range; a fake "…1234567" subscriber is correctly rejected as invalid.
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("050-234-5678", "+972502345678"),    # local IL, dashed
        ("0502345678", "+972502345678"),       # local IL, bare national
        ("+972 50-234-5678", "+972502345678"), # explicit +972, formatted
        ("972502345678", "+972502345678"),     # bare wa_id (no +)
        ("+1 650-253-0000", "+16502530000"),   # explicit non-IL country code
        ("+16502530000", "+16502530000"),      # already canonical
    ],
)
def test_normalize_valid(raw, expected):
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "hello", "12", "+", "++972", "not-a-number"],
)
def test_normalize_invalid_rejected(raw):
    with pytest.raises(InvalidPhoneNumber):
        normalize_phone(raw)


def test_wa_id_and_local_entry_collapse_to_same_key():
    # The webhook wa_id and the Host's local entry must produce the same UNIQUE key.
    assert normalize_phone("972502345678") == normalize_phone("050-234-5678")


def test_invalid_keeps_original_on_exception():
    with pytest.raises(InvalidPhoneNumber) as exc:
        normalize_phone("hello")
    assert exc.value.raw == "hello"
