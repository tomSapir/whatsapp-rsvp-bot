"""M4 webhook tests (PLAN §6/§7): signature gate, idempotency, branching, sender match.

The app under test is built by :func:`app.webhook.create_webhook_app` with a temp SQLite
and a recording ``notify`` — no live settings, no network. FastAPI's ``TestClient`` runs
background tasks before returning the response, so the ingestion side effects are visible
right after each ``client.post(...)``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.db import create_db_engine, init_db
from app.models import Invitation, Language, Message
from app.webhook import create_webhook_app

VERIFY_TOKEN = "test-verify-token"
APP_SECRET = "test-app-secret"
# Numbers must be real per libphonenumber's metadata (see test_phone.py).
KNOWN_WA_ID = "972502345678"  # stored as +972502345678 on the invitation
KNOWN_PHONE = "+972502345678"
UNKNOWN_WA_ID = "16502530000"  # valid number, but no invitation has it


@pytest.fixture
def harness(tmp_path):
    """(client, session_factory, notifications) wired to a fresh temp database."""
    engine = create_db_engine(f"sqlite:///{tmp_path / 'test.sqlite3'}")
    init_db(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    notifications: list[str] = []
    app = create_webhook_app(
        verify_token=VERIFY_TOKEN,
        app_secret=APP_SECRET,
        session_factory=session_factory,
        notify=notifications.append,
    )
    with TestClient(app) as client:
        yield client, session_factory, notifications
    engine.dispose()


def _sign(body: bytes, secret: str = APP_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _post(client: TestClient, payload: dict[str, Any], signature: str | None = ...):
    """POST a payload with a signature (computed by default; pass None to omit)."""
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if signature is ...:
        signature = _sign(body)
    if signature is not None:
        headers["X-Hub-Signature-256"] = signature
    return client.post("/webhook", content=body, headers=headers)


def _message_payload(
    wa_id: str = KNOWN_WA_ID, msg_id: str = "wamid.TEST1", text: str = "hello"
) -> dict[str, Any]:
    """A minimal but realistic Meta inbound-text event."""
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "123",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "contacts": [{"wa_id": wa_id, "profile": {"name": "Dana"}}],
                            "messages": [
                                {
                                    "from": wa_id,
                                    "id": msg_id,
                                    "timestamp": "1718000000",
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def _status_payload(msg_id: str = "wamid.OUT1") -> dict[str, Any]:
    """A delivery-receipt event for one of *our* outbound messages."""
    payload = _message_payload()
    payload["entry"][0]["changes"][0]["value"] = {
        "messaging_product": "whatsapp",
        "statuses": [{"id": msg_id, "status": "delivered", "recipient_id": KNOWN_WA_ID}],
    }
    return payload


def _add_invitation(session_factory, phone: str = KNOWN_PHONE) -> None:
    with session_factory() as session:
        session.add(Invitation(name="Dana", phone=phone, language=Language.en))
        session.commit()


def _messages(session_factory) -> list[Message]:
    with session_factory() as session:
        return session.query(Message).all()


# --- GET /webhook — subscription handshake ------------------------------------------------


def test_verify_echoes_challenge_on_token_match(harness):
    client, _, _ = harness
    response = client.get(
        "/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "1158201444",
        },
    )
    assert response.status_code == 200
    assert response.text == "1158201444"  # bare string, not JSON-quoted


def test_verify_rejects_wrong_token(harness):
    client, _, _ = harness
    response = client.get(
        "/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong",
            "hub.challenge": "1158201444",
        },
    )
    assert response.status_code == 403


# --- POST /webhook — signature gate --------------------------------------------------------


def test_valid_signature_accepted(harness):
    client, session_factory, _ = harness
    response = _post(client, _message_payload())
    assert response.status_code == 200
    assert len(_messages(session_factory)) == 1


def test_bad_signature_rejected_before_processing(harness):
    client, session_factory, notifications = harness
    response = _post(client, _message_payload(), signature="sha256=" + "0" * 64)
    assert response.status_code == 403
    assert _messages(session_factory) == []  # dropped before any DB touch
    assert notifications == []


def test_missing_signature_rejected(harness):
    client, session_factory, _ = harness
    response = _post(client, _message_payload(), signature=None)
    assert response.status_code == 403
    assert _messages(session_factory) == []


# --- POST /webhook — idempotency ------------------------------------------------------------


def test_duplicate_delivery_processed_once(harness):
    client, session_factory, notifications = harness
    payload = _message_payload(wa_id=UNKNOWN_WA_ID, msg_id="wamid.DUP")  # unknown sender

    assert _post(client, payload).status_code == 200  # both deliveries are acked 200...
    assert _post(client, payload).status_code == 200

    assert len(_messages(session_factory)) == 1  # ...but ingested exactly once
    assert len(notifications) == 1  # and the Host is notified exactly once


# --- POST /webhook — event branching --------------------------------------------------------


def test_status_callback_logged_not_processed(harness):
    client, session_factory, notifications = harness
    response = _post(client, _status_payload())
    assert response.status_code == 200
    assert _messages(session_factory) == []  # receipts never become inbound rows
    assert notifications == []


# --- POST /webhook — sender matching --------------------------------------------------------


def test_known_sender_linked_to_invitation(harness):
    client, session_factory, notifications = harness
    _add_invitation(session_factory)

    _post(client, _message_payload(wa_id=KNOWN_WA_ID, text="yes! 3 of us"))

    (message,) = _messages(session_factory)
    assert message.invitation_id is not None
    assert message.body == "yes! 3 of us"
    assert notifications == []  # known sender → no unknown-number alert


def test_unknown_sender_logged_and_notified(harness):
    client, session_factory, notifications = harness
    _add_invitation(session_factory)  # a different guest exists

    _post(client, _message_payload(wa_id=UNKNOWN_WA_ID, msg_id="wamid.UNK"))

    (message,) = _messages(session_factory)
    assert message.invitation_id is None  # logged, never auto-created
    assert len(notifications) == 1
    assert UNKNOWN_WA_ID in notifications[0]


def test_unparseable_wa_id_takes_unknown_path(harness):
    client, session_factory, notifications = harness
    _post(client, _message_payload(wa_id="000", msg_id="wamid.BAD"))

    (message,) = _messages(session_factory)
    assert message.invitation_id is None
    assert len(notifications) == 1
