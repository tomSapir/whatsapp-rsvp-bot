"""M5 state-machine tests — the PLAN §5 transition table, driven as a table (PLAN §12).

Each case sets up an invitation in a given state, feeds one reply (button or parsed free
text via :class:`StubReplyParser`), and asserts the resulting ``status`` /
``conversation_state`` / RSVP — plus the seam behaviors: the follow-up question is sent
(and audit-logged) exactly when entering ``awaiting_details``, ``null`` never overwrites,
validation failures and ambiguous messages touch nothing, every reply notifies the Host.
"""

from __future__ import annotations

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
    Invitation,
    InvitationStatus,
    Language,
    Message,
    MessageDirection,
    Rsvp,
)
from app.parser import Intent, ParsedReply, StubReplyParser
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
