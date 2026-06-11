"""M8 action tests — the write-side behind the Streamlit buttons, on fakes (PLAN §8).

Same recipe as the engine tests: temp SQLite + FakeWhatsAppClient; every send asserts the
template/state/audit-log effects the §5 transition table demands.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.orm import Session

from app.actions import (
    DEFAULT_INVITE_TEMPLATE,
    DEFAULT_NUDGE_TEMPLATE,
    DuplicatePhoneError,
    add_invitation,
    delete_invitation,
    nudge_for_details,
    re_invite,
    remind_non_responders,
    send_invites,
    update_invitation,
    upsert_event,
)
from app.db import create_db_engine, init_db
from app.models import (
    ConversationState,
    Event,
    Invitation,
    InvitationStatus,
    Language,
    Message,
    Rsvp,
)
from app.phone import InvalidPhoneNumber
from app.whatsapp import FakeWhatsAppClient


@pytest.fixture
def session(tmp_path):
    engine = create_db_engine(f"sqlite:///{tmp_path / 'test.sqlite3'}")
    init_db(engine)
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture
def whatsapp():
    return FakeWhatsAppClient()


# --- Event setup ------------------------------------------------------------------------------


def test_upsert_event_creates_then_updates_single_row(session):
    upsert_event(
        session,
        couple_name_en="Ada & Bo",
        couple_name_he="עדה ובו",
        event_date=date(2026, 7, 1),
    )
    upsert_event(
        session,
        couple_name_en="Ada & Bo!",
        couple_name_he="עדה ובו",
        event_date=date(2026, 7, 2),
        image_path="data/uploads/pic.png",
    )

    (event,) = session.query(Event).all()  # still one row — the CHECK stays happy
    assert event.couple_name_en == "Ada & Bo!"
    assert event.event_date == date(2026, 7, 2)
    assert event.image_path == "data/uploads/pic.png"


# --- Invitation CRUD ----------------------------------------------------------------------------


def test_add_invitation_canonicalizes_phone(session):
    invitation = add_invitation(
        session, name="  Dana ", phone="050-234-5678", language=Language.he
    )
    assert invitation.phone == "+972502345678"  # stored canonical, ready for wa_id matching
    assert invitation.name == "Dana"
    assert invitation.status is InvitationStatus.draft


def test_add_invitation_rejects_invalid_phone(session):
    with pytest.raises(InvalidPhoneNumber):
        add_invitation(session, name="Bad", phone="hello", language=Language.en)
    assert session.query(Invitation).count() == 0  # never stored


def test_duplicate_guard_catches_same_number_in_different_formats(session):
    add_invitation(session, name="Dana", phone="0502345678", language=Language.he)
    with pytest.raises(DuplicatePhoneError):
        add_invitation(session, name="Dana2", phone="+972 50-234-5678", language=Language.en)


def test_update_invitation_recanonicalizes_and_guards(session):
    dana = add_invitation(session, name="Dana", phone="0502345678", language=Language.he)
    omer = add_invitation(session, name="Omer", phone="0522345678", language=Language.he)

    update_invitation(session, omer, phone="+1 650-253-0000", name="Omer B.")
    assert omer.phone == "+16502530000"

    with pytest.raises(DuplicatePhoneError):
        update_invitation(session, omer, phone="050-234-5678")  # Dana's number
    assert dana.phone == "+972502345678"


def test_delete_invitation_cascades(session, whatsapp):
    invitation = add_invitation(session, name="Dana", phone="0502345678", language=Language.he)
    send_invites(session, whatsapp)
    session.add(Rsvp(invitation_id=invitation.id, attending=True, party_size=2))
    session.commit()

    delete_invitation(session, invitation)
    assert session.query(Invitation).count() == 0
    assert session.query(Rsvp).count() == 0
    assert session.query(Message).count() == 0


# --- Send invites -------------------------------------------------------------------------------


def test_send_invites_flips_drafts_and_logs(session, whatsapp):
    dana = add_invitation(session, name="Dana", phone="0502345678", language=Language.he)
    omer = add_invitation(session, name="Omer", phone="0522345678", language=Language.en)

    assert send_invites(session, whatsapp) == 2

    for invitation in (dana, omer):
        assert invitation.status is InvitationStatus.invited
        assert invitation.conversation_state is ConversationState.awaiting_yesno
        assert invitation.invited_at is not None
    assert {s.kind for s in whatsapp.sent} == {"template"}
    assert {s.payload["name"] for s in whatsapp.sent} == {DEFAULT_INVITE_TEMPLATE}
    assert {s.payload["language"] for s in whatsapp.sent} == {"he", "en"}
    assert session.query(Message).count() == 2  # audit-logged

    assert send_invites(session, whatsapp) == 0  # idempotent: no drafts left


# --- Manual remind ------------------------------------------------------------------------------


def test_remind_non_responders_targets_only_invited(session, whatsapp):
    dana = add_invitation(session, name="Dana", phone="0502345678", language=Language.he)
    omer = add_invitation(session, name="Omer", phone="0522345678", language=Language.en)
    send_invites(session, whatsapp)
    dana.status = InvitationStatus.confirmed  # Dana already replied
    session.commit()
    whatsapp.sent.clear()

    assert remind_non_responders(session, whatsapp) == 1

    (sent,) = whatsapp.sent
    assert sent.to == omer.phone
    assert omer.reminder_count == 1
    assert omer.last_reminded_at is not None


# --- Nudge for details --------------------------------------------------------------------------


def test_nudge_confirmed_but_incomplete(session, whatsapp):
    invitation = add_invitation(session, name="Dana", phone="0502345678", language=Language.he)
    invitation.status = InvitationStatus.confirmed
    invitation.conversation_state = ConversationState.done
    session.add(Rsvp(invitation_id=invitation.id, attending=True, party_size=None))
    session.commit()

    nudge_for_details(session, whatsapp, invitation)

    (sent,) = whatsapp.sent
    assert sent.payload["name"] == DEFAULT_NUDGE_TEMPLATE
    assert invitation.conversation_state is ConversationState.awaiting_details


def test_nudge_rejected_when_size_known_or_not_confirmed(session, whatsapp):
    complete = add_invitation(session, name="Dana", phone="0502345678", language=Language.he)
    complete.status = InvitationStatus.confirmed
    session.add(Rsvp(invitation_id=complete.id, attending=True, party_size=3))
    silent = add_invitation(session, name="Omer", phone="0522345678", language=Language.en)
    session.commit()

    with pytest.raises(ValueError):
        nudge_for_details(session, whatsapp, complete)
    with pytest.raises(ValueError):
        nudge_for_details(session, whatsapp, silent)
    assert whatsapp.sent == []


# --- Re-invite ----------------------------------------------------------------------------------


def test_re_invite_resets_a_declined_guest(session, whatsapp):
    invitation = add_invitation(session, name="Dana", phone="0502345678", language=Language.he)
    invitation.status = InvitationStatus.declined
    invitation.conversation_state = ConversationState.done
    invitation.reminder_count = 2
    session.add(Rsvp(invitation_id=invitation.id, attending=False))
    session.commit()

    re_invite(session, whatsapp, invitation)

    assert invitation.status is InvitationStatus.invited
    assert invitation.conversation_state is ConversationState.awaiting_yesno
    assert invitation.rsvp is None  # RSVP reset, per the §5 table
    assert invitation.invited_at is not None
    assert invitation.reminder_count == 0  # fresh chase budget for the M7 job
    assert whatsapp.sent[0].payload["name"] == DEFAULT_INVITE_TEMPLATE


def test_re_invite_rejected_for_confirmed_guest(session, whatsapp):
    invitation = add_invitation(session, name="Dana", phone="0502345678", language=Language.he)
    invitation.status = InvitationStatus.confirmed
    session.commit()
    with pytest.raises(ValueError):
        re_invite(session, whatsapp, invitation)
    assert whatsapp.sent == []
