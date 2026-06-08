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


def test_event_is_single_row(session):
    session.add(
        Event(couple_name_en="Ada & Bo", couple_name_he="עדה ובו", event_date=date(2026, 7, 1))
    )
    session.commit()
    session.add(
        Event(couple_name_en="Cy & Di", couple_name_he="סיי ודי", event_date=date(2026, 8, 1))
    )
    with pytest.raises(IntegrityError):
        session.commit()


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
