"""WhatsApp sending — an injectable client interface with a real + fake implementation.

The bot only ever *sends* through the three verbs WhatsApp gives us: pre-approved
**templates** (the first contact, outside the 24-hour window), free **text**, and
**interactive** messages (button/list replies). :class:`WhatsAppClient` is the contract; the
rest of the app depends on it, never on a concrete class, so the real Graph API client is
injected in production and :class:`FakeWhatsAppClient` (which just records sends) in tests —
no network, fully deterministic (PLAN §9, §12).

The real client speaks the WhatsApp Business Cloud API via Meta's Graph API: a ``POST`` to
``{base}/{version}/{phone_number_id}/messages`` with a Bearer token. A successful response
carries the new message id at ``messages[0].id`` — the same ``wa_message_id`` the webhook
later dedupes on, so we surface it on :class:`SendResult`.

Callers pass ``to`` already canonicalized to E.164 (see :mod:`app.phone`); the client does
not re-validate.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import httpx

_GRAPH_BASE_URL = "https://graph.facebook.com"
_DEFAULT_TIMEOUT = 30.0


@dataclass(frozen=True)
class SendResult:
    """Outcome of a successful send — the assigned id plus the raw API response."""

    wa_message_id: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class SentMessage:
    """A single recorded send (used by :class:`FakeWhatsAppClient` for assertions)."""

    to: str
    kind: str  # "template" | "text" | "interactive"
    payload: dict[str, Any]


class WhatsAppClient(ABC):
    """The send contract every implementation (real or fake) must satisfy."""

    @abstractmethod
    def send_template(
        self,
        to: str,
        template_name: str,
        language: str,
        components: list[dict[str, Any]] | None = None,
    ) -> SendResult:
        """Send a pre-approved template (e.g. the invite) in ``language`` to ``to``."""

    @abstractmethod
    def send_text(self, to: str, body: str) -> SendResult:
        """Send a free-text message (only valid inside the 24-hour session window)."""

    @abstractmethod
    def send_interactive(self, to: str, interactive: dict[str, Any]) -> SendResult:
        """Send an interactive message (the ``interactive`` object per the Cloud API)."""


class GraphWhatsAppClient(WhatsAppClient):
    """Real client — POSTs to the WhatsApp Business Cloud API (Meta Graph API).

    ``http_client`` is injectable so tests can drive it with an ``httpx.MockTransport``
    instead of hitting the network; ``base_url`` is likewise overridable.
    """

    def __init__(
        self,
        access_token: str,
        phone_number_id: str,
        api_version: str = "v21.0",
        *,
        http_client: httpx.Client | None = None,
        base_url: str = _GRAPH_BASE_URL,
    ) -> None:
        self._token = access_token
        self._url = f"{base_url}/{api_version}/{phone_number_id}/messages"
        self._http = http_client or httpx.Client(timeout=_DEFAULT_TIMEOUT)

    def _post(self, message: dict[str, Any]) -> SendResult:
        """POST a message envelope and lift the assigned ``wa_message_id`` out."""
        response = self._http.post(
            self._url,
            json={"messaging_product": "whatsapp", **message},
            headers={"Authorization": f"Bearer {self._token}"},
        )
        if response.is_error:
            # Graph puts the actual reason (error code, template name, …) in the body;
            # raise_for_status alone would discard it.
            raise httpx.HTTPStatusError(
                f"Graph API {response.status_code} for {self._url}: {response.text}",
                request=response.request,
                response=response,
            )
        data = response.json()
        return SendResult(wa_message_id=data["messages"][0]["id"], raw=data)

    def send_template(
        self,
        to: str,
        template_name: str,
        language: str,
        components: list[dict[str, Any]] | None = None,
    ) -> SendResult:
        template: dict[str, Any] = {"name": template_name, "language": {"code": language}}
        if components:
            template["components"] = components
        return self._post({"to": to, "type": "template", "template": template})

    def send_text(self, to: str, body: str) -> SendResult:
        return self._post({"to": to, "type": "text", "text": {"body": body}})

    def send_interactive(self, to: str, interactive: dict[str, Any]) -> SendResult:
        return self._post({"to": to, "type": "interactive", "interactive": interactive})


class FakeWhatsAppClient(WhatsAppClient):
    """Test double — records every send instead of calling the network.

    Inspect :attr:`sent` to assert what the app tried to send (e.g. "template X to +972…").
    Returns a deterministic, unique ``wa_message_id`` per send so callers that persist it
    (the outbound audit-log row) behave just like they would against the real API.
    """

    def __init__(self) -> None:
        self.sent: list[SentMessage] = []
        self._counter = 0

    def _record(self, to: str, kind: str, payload: dict[str, Any]) -> SendResult:
        self._counter += 1
        wa_message_id = f"wamid.fake{self._counter}"
        self.sent.append(SentMessage(to=to, kind=kind, payload=payload))
        return SendResult(wa_message_id=wa_message_id, raw={"messages": [{"id": wa_message_id}]})

    def send_template(
        self,
        to: str,
        template_name: str,
        language: str,
        components: list[dict[str, Any]] | None = None,
    ) -> SendResult:
        return self._record(
            to,
            "template",
            {"name": template_name, "language": language, "components": components},
        )

    def send_text(self, to: str, body: str) -> SendResult:
        return self._record(to, "text", {"body": body})

    def send_interactive(self, to: str, interactive: dict[str, Any]) -> SendResult:
        return self._record(to, "interactive", interactive)


def build_whatsapp_client(settings: Any = None) -> WhatsAppClient:
    """Construct the real :class:`GraphWhatsAppClient` from app settings (used by M9 wiring).

    Imported settings are read lazily here so this module stays import-side-effect-free.
    """
    if settings is None:
        from app.config import get_settings

        settings = get_settings()
    return GraphWhatsAppClient(
        access_token=settings.whatsapp_access_token,
        phone_number_id=settings.phone_number_id,
        api_version=settings.graph_api_version,
    )
