"""M5 state-machine tests — the PLAN §5 transition table, driven as a table (PLAN §12).

Each case sets up an invitation in a given state, feeds one reply (button or parsed free
text via :class:`StubReplyParser`), and asserts the resulting ``status`` /
``conversation_state`` / RSVP — plus the seam behaviors: the follow-up question is sent
(and audit-logged) exactly when entering ``awaiting_details``, ``null`` never overwrites,
validation failures and ambiguous messages touch nothing, every reply notifies the Host.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.orm import Session

from app.conversation import (
    CONFIRM_ATTENDING_PROMPTS,
    CONFIRM_DECLINED_PROMPTS,
    FOLLOW_UP_PROMPTS,
    handle_button_reply,
    handle_text_reply,
)
from app.db import create_db_engine, init_db
from app.models import (
    ConversationState,
    Event,
    Invitation,
    InvitationStatus,
    Language,
    Message,
    MessageDirection,
    Rsvp,
)
from app.parser import Intent, ParsedReply, ParserUnavailable, ReplyParser, StubReplyParser
from app.whatsapp import FakeWhatsAppClient

PHONE = "+972502345678"


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


@pytest.fixture
def notifications():
    return []


def _invitation(
    session: Session,
    *,
    attending: bool | None = None,
    party_size: int | None = None,
    language: Language = Language.en,
) -> Invitation:
    """An invited guest; ``attending`` pre-seeds an RSVP and the matching status/state."""
    if attending is None:
        status, state = InvitationStatus.invited, ConversationState.awaiting_yesno
    elif attending:
        status = InvitationStatus.confirmed
        state = (
            ConversationState.done
            if party_size is not None
            else ConversationState.awaiting_details
        )
    else:
        status, state = InvitationStatus.declined, ConversationState.done

    invitation = Invitation(
        name="Dana",
        phone=PHONE,
        language=language,
        status=status,
        conversation_state=state,
    )
    session.add(invitation)
    if attending is not None:
        session.add(
            Rsvp(invitation=invitation, attending=attending, party_size=party_size)
        )
    session.commit()
    return invitation


def _text(session, invitation, parsed, whatsapp, notifications, text="msg"):
    handle_text_reply(
        session,
        invitation,
        text,
        parser=StubReplyParser({text: parsed}),
        whatsapp=whatsapp,
        notify=notifications.append,
    )


# --- The transition table, row by row (PLAN §5) ---------------------------------------------

# (case id, prior RSVP (attending, party_size) or None, parsed reply,
#  expected: status, conversation_state, attending, party_size, guest message sent)
# `sent` is the single guest-facing message this reply triggers: None, "follow_up" (entering
#  awaiting_details), or "confirm" (entering done — a confirmation/decline acknowledgement).
TRANSITIONS = [
    (
        "button-yes-equivalent: first explicit yes, no size",
        None,
        ParsedReply(intent=Intent.rsvp_yes),
        InvitationStatus.confirmed, ConversationState.awaiting_details, True, None, "follow_up",
    ),
    (
        "first-contact free-text yes with size -> done in one shot",
        None,
        ParsedReply(intent=Intent.rsvp_yes, party_size=3, dietary="one vegan"),
        InvitationStatus.confirmed, ConversationState.done, True, 3, "confirm",
    ),
    (
        "first explicit no",
        None,
        ParsedReply(intent=Intent.rsvp_no),
        InvitationStatus.declined, ConversationState.done, False, None, "confirm",
    ),
    (
        "details after yes complete the rsvp",
        (True, None),
        ParsedReply(intent=Intent.provide_details, party_size=4),
        InvitationStatus.confirmed, ConversationState.done, True, 4, "confirm",
    ),
    (
        "yes->no flip clears party_size",
        (True, 4),
        ParsedReply(intent=Intent.rsvp_no),
        InvitationStatus.declined, ConversationState.done, False, None, None,
    ),
    (
        "no->yes flip reopens details",
        (False, None),
        ParsedReply(intent=Intent.rsvp_yes),
        InvitationStatus.confirmed, ConversationState.awaiting_details, True, None, "follow_up",
    ),
    (
        "change intent updates the size (latest reply wins)",
        (True, 4),
        ParsedReply(intent=Intent.change, party_size=5),
        InvitationStatus.confirmed, ConversationState.done, True, 5, None,
    ),
    (
        "change intent with explicit attending=false declines + clears",
        (True, 4),
        ParsedReply(intent=Intent.change, attending=False),
        InvitationStatus.declined, ConversationState.done, False, None, None,
    ),
    (
        "null never overwrites: dietary-only update keeps the size",
        (True, 4),
        ParsedReply(intent=Intent.provide_details, dietary="vegan"),
        InvitationStatus.confirmed, ConversationState.done, True, 4, None,
    ),
    (
        "question changes nothing",
        (True, 4),
        ParsedReply(intent=Intent.question),
        InvitationStatus.confirmed, ConversationState.done, True, 4, None,
    ),
    (
        "other/unintelligible changes nothing",
        (True, 4),
        ParsedReply(intent=Intent.other),
        InvitationStatus.confirmed, ConversationState.done, True, 4, None,
    ),
]


@pytest.mark.parametrize(
    "prior, parsed, status, state, attending, party_size, sent",
    [case[1:] for case in TRANSITIONS],
    ids=[case[0] for case in TRANSITIONS],
)
def test_transition_table(
    session, whatsapp, notifications, prior, parsed, status, state, attending, party_size, sent
):
    invitation = _invitation(
        session,
        attending=None if prior is None else prior[0],
        party_size=None if prior is None else prior[1],
    )

    _text(session, invitation, parsed, whatsapp, notifications)

    assert invitation.status is status
    assert invitation.conversation_state is state
    if attending is None:
        assert invitation.rsvp is None
    else:
        assert invitation.rsvp.attending is attending
        assert invitation.rsvp.party_size == party_size

    if sent is None:
        assert whatsapp.sent == []
    else:
        (message,) = whatsapp.sent
        if sent == "follow_up":
            assert message.payload["body"] == FOLLOW_UP_PROMPTS[Language.en]
        else:  # "confirm" — confirmation when attending, acknowledgement when declined
            expected = (
                CONFIRM_ATTENDING_PROMPTS[Language.en].format(n=party_size)
                if attending
                else CONFIRM_DECLINED_PROMPTS[Language.en]
            )
            assert message.payload["body"] == expected
    assert len(notifications) == 1  # every reply fires exactly one Host notification


# --- Button taps (deterministic — no parser involved) ----------------------------------------


def test_button_yes(session, whatsapp, notifications):
    invitation = _invitation(session)
    handle_button_reply(
        session, invitation, "Yes", whatsapp=whatsapp, notify=notifications.append
    )
    assert invitation.status is InvitationStatus.confirmed
    assert invitation.conversation_state is ConversationState.awaiting_details
    assert invitation.rsvp.attending is True
    assert invitation.rsvp.party_size is None  # confirmed but incomplete — never coerced


def test_button_no(session, whatsapp, notifications):
    invitation = _invitation(session)
    handle_button_reply(
        session, invitation, "No", whatsapp=whatsapp, notify=notifications.append
    )
    assert invitation.status is InvitationStatus.declined
    assert invitation.conversation_state is ConversationState.done
    assert invitation.rsvp.attending is False
    # No follow-up question, but the guest is acknowledged on entering `done`.
    (ack,) = whatsapp.sent
    assert ack.payload["body"] == CONFIRM_DECLINED_PROMPTS[Language.en]


def test_button_hebrew_yes(session, whatsapp, notifications):
    invitation = _invitation(session, language=Language.he)
    handle_button_reply(
        session, invitation, "כן", whatsapp=whatsapp, notify=notifications.append
    )
    assert invitation.status is InvitationStatus.confirmed
    assert whatsapp.sent[0].payload["body"] == FOLLOW_UP_PROMPTS[Language.he]


def test_unrecognized_button_touches_nothing(session, whatsapp, notifications):
    invitation = _invitation(session)
    handle_button_reply(
        session, invitation, "Maybe", whatsapp=whatsapp, notify=notifications.append
    )
    assert invitation.status is InvitationStatus.invited
    assert invitation.rsvp is None
    assert len(notifications) == 1 and "unrecognized" in notifications[0].lower()


# --- The follow-up question seam -------------------------------------------------------------


def test_follow_up_sent_and_audit_logged(session, whatsapp, notifications):
    invitation = _invitation(session)
    handle_button_reply(
        session, invitation, "Yes", whatsapp=whatsapp, notify=notifications.append
    )

    (sent,) = whatsapp.sent
    assert sent.to == PHONE
    assert sent.payload["body"] == FOLLOW_UP_PROMPTS[Language.en]

    outbound = [
        m for m in session.query(Message).all() if m.direction is MessageDirection.outbound
    ]
    (logged,) = outbound
    assert logged.invitation_id == invitation.id
    assert logged.wa_message_id is not None  # the fake's id, persisted like the real one


def test_repeated_yes_does_not_reask(session, whatsapp, notifications):
    invitation = _invitation(session)
    for _ in range(2):
        handle_button_reply(
            session, invitation, "Yes", whatsapp=whatsapp, notify=notifications.append
        )
    assert len(whatsapp.sent) == 1  # follow-up only on *entering* awaiting_details


# --- Guard rails ------------------------------------------------------------------------------


def test_out_of_range_party_size_not_saved(session, whatsapp, notifications):
    invitation = _invitation(session, attending=True, party_size=None)
    _text(
        session,
        invitation,
        ParsedReply(intent=Intent.provide_details, party_size=50),
        whatsapp,
        notifications,
    )
    assert invitation.rsvp.party_size is None
    assert invitation.conversation_state is ConversationState.awaiting_details
    assert any("out of the sane range" in n for n in notifications)


def test_details_before_any_yesno_touch_nothing(session, whatsapp, notifications):
    invitation = _invitation(session)  # awaiting_yesno, no RSVP yet
    _text(
        session,
        invitation,
        ParsedReply(intent=Intent.provide_details, party_size=3),
        whatsapp,
        notifications,
    )
    assert invitation.rsvp is None
    assert invitation.status is InvitationStatus.invited
    assert len(notifications) == 1 and "hasn't answered" in notifications[0]


def test_parse_failure_touches_nothing_and_notifies(session, whatsapp, notifications):
    invitation = _invitation(session, attending=True, party_size=4)
    handle_text_reply(
        session,
        invitation,
        "??",
        parser=StubReplyParser({}),  # unmapped text → ParseError
        whatsapp=whatsapp,
        notify=notifications.append,
    )
    assert invitation.rsvp.party_size == 4
    assert len(notifications) == 1 and "Couldn't understand" in notifications[0]


class _UnavailableParser(ReplyParser):
    """A parser that always fails to *reach* the model (OpenAI down / timeout)."""

    def parse(self, text: str) -> ParsedReply:
        raise ParserUnavailable("OpenAI request failed: boom")


def test_parser_unavailable_touches_nothing_and_notifies(session, whatsapp, notifications):
    """A transport/API failure must not silently drop the reply: nothing changes and the
    Host is told to handle it manually — with a message distinct from 'couldn't understand'
    so the Host knows the reply was fine and just needs re-recording."""
    invitation = _invitation(session, attending=True, party_size=4)
    handle_text_reply(
        session,
        invitation,
        "כן, נגיע 3",
        parser=_UnavailableParser(),
        whatsapp=whatsapp,
        notify=notifications.append,
    )
    assert invitation.rsvp.party_size == 4  # untouched
    assert whatsapp.sent == []  # no follow-up/confirmation went out
    assert len(notifications) == 1 and "unreachable" in notifications[0]


def test_state_committed_before_ack_send_survives_a_send_failure(session, notifications):
    """Commit-then-send (#4): if the guest ack fails to send, the RSVP is still durably
    recorded. The old send-then-commit order rolled the change back on a send failure, and
    the inbound dedup key meant a redelivery couldn't repair it."""
    invitation = _invitation(session)  # invited / awaiting_yesno, no RSVP yet

    class _SendFails(FakeWhatsAppClient):
        def send_text(self, to: str, body: str):
            raise RuntimeError("network down")

    with pytest.raises(RuntimeError):
        handle_button_reply(
            session, invitation, "כן", whatsapp=_SendFails(), notify=notifications.append
        )
    session.rollback()  # discard anything uncommitted — a committed Yes must survive this
    assert invitation.status is InvitationStatus.confirmed
    assert invitation.rsvp is not None and invitation.rsvp.attending is True


def test_party_size_on_decline_not_saved(session, whatsapp, notifications):
    invitation = _invitation(session)
    _text(
        session,
        invitation,
        ParsedReply(intent=Intent.rsvp_no, party_size=4, note="sorry, we're abroad"),
        whatsapp,
        notifications,
    )
    assert invitation.rsvp.attending is False
    assert invitation.rsvp.party_size is None  # the invariant: declines carry no head-count
    assert invitation.rsvp.note == "sorry, we're abroad"  # but the note is kept


def _seed_event(session, **location):
    session.add(
        Event(
            partner1_first_en="Ada",
            partner1_last_en="Cohen",
            partner2_first_en="Bo",
            partner2_last_en="Levi",
            partner1_first_he="עדה",
            partner1_last_he="כהן",
            partner2_first_he="בו",
            partner2_last_he="לוי",
            event_date=date(2026, 7, 1),
            **location,
        )
    )
    session.commit()


def test_attending_confirmation_includes_location_links(session, whatsapp, notifications):
    _seed_event(session, location_name="Beit Yaar", location_lat=32.0853, location_lng=34.7818)
    invitation = _invitation(session)  # awaiting_yesno, no RSVP yet

    _text(  # one-shot yes-with-count → enters `done` attending → confirmation sent
        session, invitation, ParsedReply(intent=Intent.rsvp_yes, party_size=2), whatsapp, notifications
    )

    (sent,) = whatsapp.sent
    body = sent.payload["body"]
    assert CONFIRM_ATTENDING_PROMPTS[Language.en].format(n=2) in body
    assert "Beit Yaar" in body
    assert "https://waze.com/ul?ll=32.0853,34.7818&navigate=yes" in body
    assert "https://www.google.com/maps/search/?api=1&query=32.0853%2C34.7818" in body


def test_decline_confirmation_has_no_location_links(session, whatsapp, notifications):
    _seed_event(session, location_name="Beit Yaar", location_lat=32.0853, location_lng=34.7818)
    invitation = _invitation(session)

    _text(session, invitation, ParsedReply(intent=Intent.rsvp_no), whatsapp, notifications)

    (sent,) = whatsapp.sent
    assert sent.payload["body"] == CONFIRM_DECLINED_PROMPTS[Language.en]  # no directions on a no


def test_confirmation_without_event_location_is_unchanged(session, whatsapp, notifications):
    _seed_event(session)  # event exists but no location set
    invitation = _invitation(session)

    _text(
        session, invitation, ParsedReply(intent=Intent.rsvp_yes, party_size=2), whatsapp, notifications
    )

    (sent,) = whatsapp.sent
    assert sent.payload["body"] == CONFIRM_ATTENDING_PROMPTS[Language.en].format(n=2)


def test_question_notification_carries_the_text(session, whatsapp, notifications):
    invitation = _invitation(session, attending=True, party_size=2)
    _text(
        session,
        invitation,
        ParsedReply(intent=Intent.question),
        whatsapp,
        notifications,
        text="is there parking?",
    )
    assert "is there parking?" in notifications[0]
