"""Webhook ingestion — the public HTTP door Meta knocks on (PLAN §6/§7).

Two endpoints, both created by :func:`create_webhook_router`:

* ``GET /webhook`` — Meta's one-time subscription handshake: echo ``hub.challenge`` back
  *only* when ``hub.verify_token`` matches ours.
* ``POST /webhook`` — every inbound event. Four robustness rules run **in order, before
  any per-message logic** (the endpoint is public and Meta delivers at-least-once):

  1. **Signature gate first** — recompute ``X-Hub-Signature-256`` (HMAC-SHA256 of the
     **raw body**, keyed with the App Secret), constant-time compare, drop on mismatch —
     before JSON parsing, before the DB.
  2. **Ack fast, then process** — return ``200`` immediately and run the (later: OpenAI)
     processing as a background task so Meta never times out and retries.
  3. **Idempotency** — insert the ``Message`` row first; a re-delivered event hits the
     UNIQUE ``wa_message_id`` constraint → stop (no re-process, no re-notify).
  4. **Event branching** — ``value.messages[]`` are real inbound (processed);
     ``value.statuses[]`` are delivery receipts for *our* sends (logged, ignored).

Sender matching: the webhook delivers ``wa_id`` as bare digits (``972541234567``);
normalize it to E.164 via :mod:`app.phone` and look up the Invitation by ``phone``. No
match (or unparseable) → log the message with no invitation and notify the Host — never
auto-create an Invitation.

Everything is built through a factory taking explicit dependencies (verify token, app
secret, sessionmaker, notify callable, WhatsApp client, reply parser), so tests run it
against a temp SQLite with fakes and the production wiring (``app/main.py``) passes the
real clients built from settings. After ingestion, a matched message is routed into the
M5 conversation engine: button/interactive replies deterministically, text through the
parser. ``whatsapp``/``parser`` are optional — without them the router ingests only
(M4 behavior), which keeps the ingestion tests independent of the engine.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.conversation import handle_button_reply, handle_text_reply
from app.models import Invitation, Message, MessageDirection, MessageType
from app.parser import ReplyParser
from app.phone import InvalidPhoneNumber, normalize_phone
from app.whatsapp import WhatsAppClient

logger = logging.getLogger(__name__)

# How the Host is told something needs attention; M6 swaps the default for the feed.
Notifier = Callable[[str], None]

# Meta inbound `type` → our audit-log enum. Anything exotic (image, audio, sticker, …)
# is logged as text with a None body — the raw JSON keeps the full original.
_MESSAGE_TYPES = {
    "text": MessageType.text,
    "button": MessageType.button,
    "interactive": MessageType.interactive,
}


def verify_signature(app_secret: str, raw_body: bytes, signature_header: str | None) -> bool:
    """True iff ``signature_header`` is the HMAC-SHA256 of ``raw_body`` under our secret.

    The header format is ``sha256=<hex digest>``. Comparison uses
    :func:`hmac.compare_digest` so the check takes the same time whether the forgery is
    wrong in the first byte or the last (no timing oracle).
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header.removeprefix("sha256="))


def _iter_change_values(payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield each ``entry[].changes[].value`` object in a webhook payload."""
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value")
            if isinstance(value, dict):
                yield value


def _extract_body(message: dict[str, Any]) -> str | None:
    """Pull the human-readable text out of an inbound message, per Meta type."""
    match message.get("type"):
        case "text":
            return message.get("text", {}).get("body")
        case "button":  # template quick-reply tap
            return message.get("button", {}).get("text")
        case "interactive":
            interactive = message.get("interactive", {})
            reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
            return reply.get("title")
    return None


def _parse_timestamp(message: dict[str, Any]) -> datetime | None:
    """Meta's ``timestamp`` is a unix-epoch string; convert to naive UTC (column style)."""
    raw = message.get("timestamp")
    try:
        return datetime.fromtimestamp(int(raw), tz=timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _match_invitation(session: Session, wa_id: str | None) -> Invitation | None:
    """Resolve a webhook ``wa_id`` (bare digits) to an Invitation, or None.

    ``normalize_phone`` treats digits without ``+`` as international, so the stored
    canonical E.164 and the inbound sender collapse to the same string. A wa_id that
    doesn't parse falls into the same unknown-sender path as a no-match.
    """
    if not wa_id:
        return None
    try:
        phone = normalize_phone(wa_id)
    except InvalidPhoneNumber:
        return None
    return session.execute(
        select(Invitation).where(Invitation.phone == phone)
    ).scalar_one_or_none()


def _ingest_message(
    message: dict[str, Any],
    session_factory: sessionmaker[Session],
    notify: Notifier,
    whatsapp: WhatsAppClient | None = None,
    parser: ReplyParser | None = None,
) -> None:
    """Ingest one inbound message: match sender, insert the audit row, then route it.

    The insert is the idempotency gate (PLAN §6): a re-delivered event collides on the
    UNIQUE ``wa_message_id`` and we stop — crucially *before* the unknown-sender notify
    and the conversation engine, so a duplicate never re-notifies or double-processes.
    """
    wa_message_id = message.get("id")
    wa_id = message.get("from")
    body = _extract_body(message)
    message_type = message.get("type")

    with session_factory() as session:
        invitation = _match_invitation(session, wa_id)
        session.add(
            Message(
                invitation_id=invitation.id if invitation else None,
                direction=MessageDirection.inbound,
                type=_MESSAGE_TYPES.get(message_type, MessageType.text),
                body=body,
                wa_message_id=wa_message_id,
                timestamp=_parse_timestamp(message),
                raw_json=json.dumps(message, ensure_ascii=False),
            )
        )
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            # Only the wa_message_id UNIQUE means a Meta re-delivery. Any other constraint
            # (a future schema change) is a real error — don't silently misclassify it as a
            # duplicate and drop the message; let it surface.
            if "wa_message_id" not in str(exc.orig):
                raise
            logger.info("duplicate webhook delivery for %s — skipping", wa_message_id)
            return

        if invitation is None:
            notify(f"📩 Message from an unknown number ({wa_id}) — no invitation matches.")
            return

        if whatsapp is None or parser is None:
            return  # ingestion-only wiring (M4 tests); production always passes both

        # Route into the M5 conversation engine, inside the same session. This runs in a
        # background task *after* the 200 and *after* the idempotency row was committed
        # above — so an unhandled exception here would just be logged by Starlette and the
        # reply lost for good (Meta won't redeliver, and the dedup row means a manual
        # redelivery is skipped). Guard it: on any failure roll back the half-applied writes
        # and tell the Host, so a reply is never silently dropped.
        try:
            if message_type in ("button", "interactive") and body:
                handle_button_reply(
                    session, invitation, body, whatsapp=whatsapp, notify=notify
                )
            elif message_type == "text" and body:
                handle_text_reply(
                    session, invitation, body, parser=parser, whatsapp=whatsapp, notify=notify
                )
            else:
                logger.info(
                    "unhandled inbound type %r from %s — logged only", message_type, wa_id
                )
        except Exception:
            logger.exception("failed to process reply from %s", invitation.phone)
            session.rollback()  # the dedup row is committed separately, so it survives
            notify(
                f"⚠️ Couldn't process the reply from {invitation.name} "
                f"({invitation.phone}) — something went wrong on our side; please check "
                "with them directly."
            )


def process_payload(
    payload: dict[str, Any],
    session_factory: sessionmaker[Session],
    notify: Notifier,
    whatsapp: WhatsAppClient | None = None,
    parser: ReplyParser | None = None,
) -> None:
    """Process one verified webhook payload: statuses are logged, messages ingested.

    Runs *after* the 200 was sent (background task), so nothing here can make Meta
    time out and re-deliver.
    """
    for value in _iter_change_values(payload):
        for status in value.get("statuses", []):
            logger.info(
                "status callback: %s is %s", status.get("id"), status.get("status")
            )
        for message in value.get("messages", []):
            _ingest_message(message, session_factory, notify, whatsapp, parser)


def create_webhook_router(
    *,
    verify_token: str,
    app_secret: str,
    session_factory: sessionmaker[Session],
    notify: Notifier | None = None,
    whatsapp: WhatsAppClient | None = None,
    parser: ReplyParser | None = None,
) -> APIRouter:
    """Build the webhook router with its dependencies bound (tests pass fakes; M9 wires real)."""
    notify = notify or (lambda text: logger.warning("host notification: %s", text))
    router = APIRouter()

    @router.get("/webhook")
    def verify_subscription(
        hub_mode: str | None = Query(default=None, alias="hub.mode"),
        hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
        hub_challenge: str = Query(default="", alias="hub.challenge"),
    ) -> PlainTextResponse:
        # Meta expects the bare challenge string back (not JSON-quoted) on a token match.
        if hub_mode == "subscribe" and hmac.compare_digest(
            hub_verify_token or "", verify_token
        ):
            return PlainTextResponse(hub_challenge)
        raise HTTPException(status_code=403, detail="verify token mismatch")

    @router.post("/webhook")
    async def receive_event(
        request: Request, background_tasks: BackgroundTasks
    ) -> dict[str, str]:
        raw_body = await request.body()  # raw bytes, *before* any JSON parsing
        if not verify_signature(
            app_secret, raw_body, request.headers.get("X-Hub-Signature-256")
        ):
            raise HTTPException(status_code=403, detail="invalid signature")
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="invalid JSON")
        background_tasks.add_task(
            process_payload, payload, session_factory, notify, whatsapp, parser
        )
        return {"status": "received"}  # ack fast; processing continues in the background

    return router


def create_webhook_app(
    *,
    verify_token: str,
    app_secret: str,
    session_factory: sessionmaker[Session],
    notify: Notifier | None = None,
    whatsapp: WhatsAppClient | None = None,
    parser: ReplyParser | None = None,
    lifespan: Any = None,
) -> FastAPI:
    """A FastAPI app exposing just the webhook (M9 mounts this with real settings)."""
    app = FastAPI(lifespan=lifespan)
    app.include_router(
        create_webhook_router(
            verify_token=verify_token,
            app_secret=app_secret,
            session_factory=session_factory,
            notify=notify,
            whatsapp=whatsapp,
            parser=parser,
        )
    )
    return app
