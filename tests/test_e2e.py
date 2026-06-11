"""M9 end-to-end webhook tests — the whole pipeline through FastAPI's TestClient.

Each test plays Meta: a signed POST with a realistic payload hits ``/webhook``, flows
through the signature gate → ingestion → sender matching → conversation engine → RSVP
writes → follow-up sends → activity feed. Everything external is the project's own fake
(FakeWhatsAppClient, StubReplyParser, temp SQLite) — the exact wiring ``app/main.py``
does with the real clients.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.conversation import FOLLOW_UP_PROMPTS
from app.db import create_db_engine, init_db
from app.models import (
    ConversationState,
    Invitation,
    InvitationStatus,
    Language,
    Message,
    Rsvp,
)
from app.notify import FeedNotifier, recent_notifications
from app.parser import Intent, ParsedReply, StubReplyParser
from app.webhook import create_webhook_app
from app.whatsapp import FakeWhatsAppClient

APP_SECRET = "e2e-secret"
VERIFY_TOKEN = "e2e-verify"
DANA_WA_ID = "972502345678"  # Hebrew guest
DANA_PHONE = "+972502345678"
OMER_WA_ID = "972522345678"  # English guest
OMER_PHONE = "+972522345678"

# What the "OpenAI" stub understands in these tests.
STUBBED_REPLIES = {
    "כן! נגיע 3, אחת צמחונית": ParsedReply(
        intent=Intent.rsvp_yes, attending=True, party_size=3, dietary="one vegetarian"
    ),
    "sadly we can't make it": ParsedReply(intent=Intent.rsvp_no, attending=False),
    "is there parking?": ParsedReply(intent=Intent.question),
}


@pytest.fixture
def harness(tmp_path):
    engine = create_db_engine(f"sqlite:///{tmp_path / 'e2e.sqlite3'}")
    init_db(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    with session_factory() as session:
        session.add_all(
            [
                Invitation(
                    name="Dana",
                    phone=DANA_PHONE,
                    language=Language.he,
                    status=InvitationStatus.invited,
                    conversation_state=ConversationState.awaiting_yesno,
                ),
                Invitation(
                    name="Omer",
                    phone=OMER_PHONE,
                    language=Language.en,
                    status=InvitationStatus.invited,
                    conversation_state=ConversationState.awaiting_yesno,
                ),
            ]
        )
        session.commit()

    whatsapp = FakeWhatsAppClient()
    app = create_webhook_app(
        verify_token=VERIFY_TOKEN,
        app_secret=APP_SECRET,
        session_factory=session_factory,
        notify=FeedNotifier(session_factory),
        whatsapp=whatsapp,
        parser=StubReplyParser(STUBBED_REPLIES),
    )
    with TestClient(app) as client:
        yield client, session_factory, whatsapp
    engine.dispose()


def _post(client: TestClient, payload: dict[str, Any]):
    body = json.dumps(payload, ensure_ascii=False).encode()
    signature = "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    response = client.post(
        "/webhook",
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": signature},
    )
    assert response.status_code == 200
    return response


def _event(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "1", "changes": [{"field": "messages", "value": value}]}],
    }


def _text_message(wa_id: str, msg_id: str, text: str) -> dict[str, Any]:
    return _event(
        {
            "messaging_product": "whatsapp",
            "messages": [
                {"from": wa_id, "id": msg_id, "timestamp": "1718000000",
                 "type": "text", "text": {"body": text}},
            ],
        }
    )


def _button_message(wa_id: str, msg_id: str, text: str) -> dict[str, Any]:
    return _event(
        {
            "messaging_product": "whatsapp",
            "messages": [
                {"from": wa_id, "id": msg_id, "timestamp": "1718000000",
                 "type": "button", "button": {"payload": text, "text": text}},
            ],
        }
    )


def _guest(session_factory, phone: str) -> Invitation:
    with session_factory() as session:
        return session.query(Invitation).filter_by(phone=phone).one()


def _feed(session_factory) -> list[str]:
    with session_factory() as session:
        return [n.text for n in recent_notifications(session)]


# --- The five scenarios from STEPS M9.2 -------------------------------------------------------


def test_button_yes_end_to_end(harness):
    client, session_factory, whatsapp = harness
    _post(client, _button_message(DANA_WA_ID, "wamid.BTN1", "כן"))

    dana = _guest(session_factory, DANA_PHONE)
    assert dana.status is InvitationStatus.confirmed
    assert dana.conversation_state is ConversationState.awaiting_details

    (follow_up,) = whatsapp.sent  # the Hebrew follow-up question went out
    assert follow_up.to == DANA_PHONE
    assert follow_up.payload["body"] == FOLLOW_UP_PROMPTS[Language.he]

    with session_factory() as session:
        rsvp = session.query(Rsvp).one()
        assert rsvp.attending is True and rsvp.party_size is None
        assert session.query(Message).count() == 2  # inbound button + outbound follow-up
    assert any("Dana" in n and "coming" in n for n in _feed(session_factory))


def test_button_no_end_to_end(harness):
    client, session_factory, whatsapp = harness
    _post(client, _button_message(OMER_WA_ID, "wamid.BTN2", "No"))

    omer = _guest(session_factory, OMER_PHONE)
    assert omer.status is InvitationStatus.declined
    assert omer.conversation_state is ConversationState.done
    assert whatsapp.sent == []  # no follow-up after a decline
    assert any("declined" in n for n in _feed(session_factory))


def test_hebrew_free_text_completes_rsvp_in_one_shot(harness):
    client, session_factory, _ = harness
    _post(client, _text_message(DANA_WA_ID, "wamid.HE1", "כן! נגיע 3, אחת צמחונית"))

    dana = _guest(session_factory, DANA_PHONE)
    assert dana.status is InvitationStatus.confirmed
    assert dana.conversation_state is ConversationState.done
    with session_factory() as session:
        rsvp = session.query(Rsvp).one()
        assert (rsvp.attending, rsvp.party_size, rsvp.dietary) == (True, 3, "one vegetarian")


def test_english_free_text_decline(harness):
    client, session_factory, _ = harness
    _post(client, _text_message(OMER_WA_ID, "wamid.EN1", "sadly we can't make it"))

    omer = _guest(session_factory, OMER_PHONE)
    assert omer.status is InvitationStatus.declined
    with session_factory() as session:
        assert session.query(Rsvp).one().attending is False


def test_question_routes_to_host_changes_nothing(harness):
    client, session_factory, whatsapp = harness
    _post(client, _text_message(DANA_WA_ID, "wamid.Q1", "is there parking?"))

    dana = _guest(session_factory, DANA_PHONE)
    assert dana.status is InvitationStatus.invited  # untouched
    assert whatsapp.sent == []
    assert any("is there parking?" in n for n in _feed(session_factory))


def test_status_callback_ignored(harness):
    client, session_factory, whatsapp = harness
    _post(
        client,
        _event(
            {
                "messaging_product": "whatsapp",
                "statuses": [
                    {"id": "wamid.OUT", "status": "delivered", "recipient_id": DANA_WA_ID}
                ],
            }
        ),
    )
    with session_factory() as session:
        assert session.query(Message).count() == 0
        assert session.query(Rsvp).count() == 0


def test_unknown_number_logged_and_notified(harness):
    client, session_factory, whatsapp = harness
    _post(client, _text_message("16502530000", "wamid.UNK", "hello?"))

    with session_factory() as session:
        (message,) = session.query(Message).all()
        assert message.invitation_id is None
        assert session.query(Rsvp).count() == 0
    assert whatsapp.sent == []
    assert any("unknown number" in n for n in _feed(session_factory))


def test_duplicate_delivery_fully_idempotent(harness):
    client, session_factory, whatsapp = harness
    payload = _button_message(DANA_WA_ID, "wamid.DUP", "כן")
    _post(client, payload)
    _post(client, payload)  # Meta re-delivers the exact same event

    with session_factory() as session:
        assert session.query(Rsvp).count() == 1
        inbound = [m for m in session.query(Message).all() if m.wa_message_id == "wamid.DUP"]
        assert len(inbound) == 1
    assert len(whatsapp.sent) == 1  # follow-up asked once, not twice
    assert len([n for n in _feed(session_factory) if "coming" in n]) == 1
