"""Tests for the WhatsApp client seam (PLAN §9, §12 · M3).

The fake is the primary test double (records sends, no network). The real
:class:`GraphWhatsAppClient` is exercised offline with an ``httpx.MockTransport`` to assert
it builds the right URL, auth header, and JSON envelope — without ever leaving the process.
"""

import json

import httpx
import pytest

from app.whatsapp import FakeWhatsAppClient, GraphWhatsAppClient

RECIPIENT = "+972502345678"


# --- FakeWhatsAppClient -----------------------------------------------------------------


def test_fake_records_template_send():
    client = FakeWhatsAppClient()
    result = client.send_template(RECIPIENT, "invite_he", "he")

    assert len(client.sent) == 1
    sent = client.sent[0]
    assert sent.kind == "template"
    assert sent.to == RECIPIENT
    assert sent.payload["name"] == "invite_he"
    assert sent.payload["language"] == "he"
    assert result.wa_message_id  # a non-empty id was returned


def test_fake_records_text_and_interactive_in_order():
    client = FakeWhatsAppClient()
    client.send_text(RECIPIENT, "Hello!")
    client.send_interactive(RECIPIENT, {"type": "button", "body": {"text": "Coming?"}})

    assert [m.kind for m in client.sent] == ["text", "interactive"]
    assert client.sent[0].payload["body"] == "Hello!"
    assert client.sent[1].payload["body"]["text"] == "Coming?"


def test_fake_returns_unique_message_ids():
    client = FakeWhatsAppClient()
    first = client.send_text(RECIPIENT, "a")
    second = client.send_text(RECIPIENT, "b")
    assert first.wa_message_id != second.wa_message_id


# --- GraphWhatsAppClient (offline via MockTransport) ------------------------------------


def test_graph_client_builds_correct_request():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"messaging_product": "whatsapp", "messages": [{"id": "wamid.REAL123"}]}
        )

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = GraphWhatsAppClient(
        access_token="TOKEN", phone_number_id="123456", api_version="v21.0", http_client=http
    )

    result = client.send_template(RECIPIENT, "invite_he", "he")

    assert result.wa_message_id == "wamid.REAL123"
    assert captured["url"] == "https://graph.facebook.com/v21.0/123456/messages"
    assert captured["auth"] == "Bearer TOKEN"
    body = captured["body"]
    assert body["messaging_product"] == "whatsapp"
    assert body["to"] == RECIPIENT
    assert body["type"] == "template"
    assert body["template"] == {"name": "invite_he", "language": {"code": "he"}}


def test_graph_client_sends_text_envelope():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"messages": [{"id": "wamid.T"}]})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = GraphWhatsAppClient("T", "1", http_client=http)

    client.send_text(RECIPIENT, "Hi there")

    assert captured["body"]["type"] == "text"
    assert captured["body"]["text"] == {"body": "Hi there"}


def test_graph_client_raises_on_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "bad request"}})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = GraphWhatsAppClient("T", "1", http_client=http)

    with pytest.raises(httpx.HTTPStatusError):
        client.send_text(RECIPIENT, "x")
