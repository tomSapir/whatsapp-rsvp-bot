"""M6 feed tests: every host-facing event type lands in the activity feed (PLAN §6 · Q2).

Rather than calling the notifier in a vacuum, each event is produced by the *real* call
site that fires it — the M5 conversation engine or the M4 webhook ingestion — with
:class:`FeedNotifier` injected where those layers expect a ``notify`` callable. That
proves the seam fits without any call-site changes.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import sessionmaker

from app.conversation import handle_button_reply, handle_text_reply
from app.db import create_db_engine, init_db
from app.models import ConversationState, Invitation, InvitationStatus, Language, Rsvp
from app.notify import FeedNotifier, recent_notifications
from app.parser import Intent, ParsedReply, StubReplyParser
from app.webhook import process_payload
from app.whatsapp import FakeWhatsAppClient

PHONE = "+972502345678"


@pytest.fixture
def session_factory(tmp_path):
    engine = create_db_engine(f"sqlite:///{tmp_path / 'test.sqlite3'}")
    init_db(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False, future=True)
    engine.dispose()


@pytest.fixture
def notifier(session_factory):
    return FeedNotifier(session_factory)


def _feed(session_factory) -> list[str]:
    with session_factory() as session:
        return [n.text for n in recent_notifications(session)]


def _add_invitation(session, *, attending: bool | None = None, party_size: int | None = None):
    invitation = Invitation(
        name="Dana",
        phone=PHONE,
        language=Language.en,
        status=InvitationStatus.invited if attending is None else InvitationStatus.confirmed,
        conversation_state=ConversationState.awaiting_yesno
        if attending is None
        else ConversationState.done,
    )
    session.add(invitation)
    if attending is not None:
        session.add(Rsvp(invitation=invitation, attending=attending, party_size=party_size))
    session.commit()
    return invitation


# --- Each event type produces a feed entry ---------------------------------------------------


def test_reply_lands_in_feed(session_factory, notifier):
    with session_factory() as session:
        invitation = _add_invitation(session)
        handle_button_reply(
            session, invitation, "Yes", whatsapp=FakeWhatsAppClient(), notify=notifier
        )

    (entry,) = _feed(session_factory)
    assert "Dana" in entry and "coming" in entry


def test_rsvp_change_lands_in_feed(session_factory, notifier):
    with session_factory() as session:
        invitation = _add_invitation(session, attending=True, party_size=4)
        handle_text_reply(
            session,
            invitation,
            "we can't make it anymore",
            parser=StubReplyParser(
                default=ParsedReply(intent=Intent.change, attending=False)
            ),
            whatsapp=FakeWhatsAppClient(),
            notify=notifier,
        )

    (entry,) = _feed(session_factory)
    assert "declined" in entry


def test_unknown_number_lands_in_feed(session_factory, notifier):
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": "16502530000",
                                    "id": "wamid.UNK",
                                    "type": "text",
                                    "text": {"body": "hi"},
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }
    process_payload(payload, session_factory, notifier)

    (entry,) = _feed(session_factory)
    assert "unknown number" in entry and "16502530000" in entry


def test_question_lands_in_feed(session_factory, notifier):
    with session_factory() as session:
        invitation = _add_invitation(session, attending=True, party_size=2)
        handle_text_reply(
            session,
            invitation,
            "is there parking?",
            parser=StubReplyParser(default=ParsedReply(intent=Intent.question)),
            whatsapp=FakeWhatsAppClient(),
            notify=notifier,
        )

    (entry,) = _feed(session_factory)
    assert "is there parking?" in entry


def test_validation_failure_lands_in_feed(session_factory, notifier):
    with session_factory() as session:
        invitation = _add_invitation(session, attending=True, party_size=None)
        handle_text_reply(
            session,
            invitation,
            "we are 50",
            parser=StubReplyParser(
                default=ParsedReply(intent=Intent.provide_details, party_size=50)
            ),
            whatsapp=FakeWhatsAppClient(),
            notify=notifier,
        )

    feed = _feed(session_factory)
    assert any("out of the sane range" in entry for entry in feed)


# --- Feed ordering ----------------------------------------------------------------------------


def test_feed_is_newest_first_and_limited(session_factory, notifier):
    for i in range(5):
        notifier(f"event {i}")

    with session_factory() as session:
        assert [n.text for n in recent_notifications(session, limit=3)] == [
            "event 4",
            "event 3",
            "event 2",
        ]
