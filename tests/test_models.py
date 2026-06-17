"""Constraint tests for the M1 data model (PLAN §5).

These assert the domain invariants are enforced by the **database**, not just the ORM:
unique phone, unique ``wa_message_id``, the ``attending=false ⇒ party_size IS NULL`` check,
one RSVP per invitation, the single-row Event guard, and that ``foreign_keys=ON`` actually
rejects orphan FKs. Each test runs against a fresh throwaway file-backed SQLite.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import create_db_engine, init_db
from app.models import (
    Event,
    Invitation,
    Language,
    Message,
    MessageDirection,
    MessageType,
    Rsvp,
)


@pytest.fixture
def session(tmp_path):
    """A Session on a fresh file-backed SQLite with the schema created.

    File-backed (not ``:memory:``) so it behaves like the real database, including the WAL
    and ``foreign_keys=ON`` pragmas wired in by :func:`app.db.create_db_engine`.
    """
    engine = create_db_engine(f"sqlite:///{tmp_path / 'test.sqlite3'}")
    init_db(engine)
    with Session(engine) as s:
        yield s
    engine.dispose()


def _invitation(phone: str = "+972541234567", **kw) -> Invitation:
    return Invitation(name="Test Guest", phone=phone, language=Language.en, **kw)


def _message(wa_message_id: str | None = "wamid.AAA", **kw) -> Message:
    return Message(
        direction=MessageDirection.inbound,
        type=MessageType.text,
        wa_message_id=wa_message_id,
        **kw,
    )


def test_duplicate_phone_rejected(session):
    session.add(_invitation(phone="+972541110000"))
    session.commit()
    session.add(_invitation(phone="+972541110000"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_duplicate_wa_message_id_rejected(session):
    session.add(_message(wa_message_id="wamid.DUP"))
    session.commit()
    session.add(_message(wa_message_id="wamid.DUP"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_declined_rsvp_cannot_carry_party_size(session):
    inv = _invitation()
    session.add(inv)
    session.commit()
    session.add(Rsvp(invitation_id=inv.id, attending=False, party_size=3))
    with pytest.raises(IntegrityError):
        session.commit()


def test_declined_rsvp_with_null_party_size_ok(session):
    inv = _invitation()
    session.add(inv)
    session.commit()
    session.add(Rsvp(invitation_id=inv.id, attending=False, party_size=None))
    session.commit()
    assert session.get(Rsvp, 1).party_size is None


def test_attending_rsvp_with_party_size_ok(session):
    inv = _invitation()
    session.add(inv)
    session.commit()
    session.add(Rsvp(invitation_id=inv.id, attending=True, party_size=4))
    session.commit()
    assert session.get(Rsvp, 1).party_size == 4


def test_one_rsvp_per_invitation(session):
    inv = _invitation()
    session.add(inv)
    session.commit()
    session.add(Rsvp(invitation_id=inv.id, attending=True, party_size=2))
    session.commit()
    session.add(Rsvp(invitation_id=inv.id, attending=False))
    with pytest.raises(IntegrityError):
        session.commit()


def _event(**overrides):
    fields = dict(
        partner1_first_en="Ada",
        partner1_last_en="Cohen",
        partner2_first_en="Bo",
        partner2_last_en="Levi",
        partner1_first_he="עדה",
        partner1_last_he="כהן",
        partner2_first_he="בו",
        partner2_last_he="לוי",
        event_date=date(2026, 7, 1),
    )
    fields.update(overrides)
    return Event(**fields)


def test_event_is_single_row(session):
    session.add(_event())
    session.commit()
    session.add(_event(partner1_first_en="Cy", event_date=date(2026, 8, 1)))
    with pytest.raises(IntegrityError):
        session.commit()


def test_event_location_links_prefer_coordinates(session):
    event = _event(location_name="Beit Yaar, Tel Aviv", location_lat=32.0853, location_lng=34.7818)
    session.add(event)
    session.commit()

    assert event.has_location and event.has_coordinates
    # Coordinates win over the address: Waze routes (navigate=yes), Maps pins the exact point.
    assert event.waze_url == "https://waze.com/ul?ll=32.0853,34.7818&navigate=yes"
    assert event.google_maps_url == (
        "https://www.google.com/maps/search/?api=1&query=32.0853%2C34.7818"
    )


def test_event_location_links_fall_back_to_address(session):
    event = _event(location_name="Beit Yaar, Tel Aviv")  # no coordinates
    session.add(event)
    session.commit()

    assert event.has_location and not event.has_coordinates
    # Address-only: both links url-encode the venue text as a query.
    assert event.waze_url == "https://waze.com/ul?q=Beit%20Yaar%2C%20Tel%20Aviv"
    assert event.google_maps_url == (
        "https://www.google.com/maps/search/?api=1&query=Beit%20Yaar%2C%20Tel%20Aviv"
    )


def test_event_without_location_has_no_links(session):
    event = _event()
    session.add(event)
    session.commit()

    assert not event.has_location
    assert event.waze_url is None
    assert event.google_maps_url is None


def test_foreign_key_enforced(session):
    # PRAGMA foreign_keys=ON (set in create_db_engine) must reject an orphan FK.
    session.add(Rsvp(invitation_id=999, attending=True, party_size=1))
    with pytest.raises(IntegrityError):
        session.commit()


def test_unknown_number_message_allowed(session):
    # A reply from an unknown number is logged with no invitation (PLAN §6).
    session.add(_message(wa_message_id="wamid.UNK", invitation_id=None))
    session.commit()
    assert session.query(Message).count() == 1


def test_multiple_null_wa_message_ids_allowed(session):
    # NULLs are distinct under SQLite UNIQUE, so outbound rows without a Meta id don't clash.
    session.add(_message(wa_message_id=None))
    session.add(_message(wa_message_id=None))
    session.commit()
    assert session.query(Message).count() == 2
