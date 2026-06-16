"""Conversation state machine — turns one inbound reply into status/state/RSVP writes.

Implements the transition table of PLAN §5 and the free-text rules of PLAN §6:

* **Button taps are deterministic** — Yes/No map straight to the table, no LLM involved.
* **Free text keys off the parser's closed ``intent``**, never off ``confidence`` (which is
  logged only). ``question``/``other`` (or a parse failure) **touch nothing** — log + notify
  the Host, who answers personally.
* **``null`` never overwrites** — only non-null parsed fields update the RSVP.
* **``attending`` flips only on an explicit statement**: the ``rsvp_yes``/``rsvp_no``
  intents, or a non-null ``attending`` on ``change``/``provide_details`` (the parser is
  instructed to set it only on an unambiguous yes/no).
* **Latest reply always wins** (no time limit); a Yes→No flip **clears ``party_size``**,
  preserving the DB invariant ``attending=false ⇒ party_size IS NULL``. History stays in
  the ``messages`` audit log.
* **``party_size`` is validated** (1–20) before writing; an out-of-range value is not
  saved and the Host is notified.
* ``status``, ``conversation_state``, and the RSVP are written together in **one commit**.

When a reply lands the conversation in ``awaiting_details`` (a Yes without a head-count),
the bot sends the follow-up question ("How many of you? …") in the invitation's language —
allowed as free text because the guest's own message just reopened the 24h window — and
logs it to the ``messages`` audit log. The follow-up is sent only when *entering* the
state, so a repeated Yes doesn't re-ask.

Symmetrically, when a reply lands the conversation in ``done`` — a one-shot yes-with-count,
a completed head-count, or a decline — the bot sends a short confirmation so the guest
always hears back rather than the conversation ending in silence. Like the follow-up it
fires only when *entering* ``done`` (so a later edit while already done doesn't re-confirm)
and is logged to the audit log.

Dependencies (parser, WhatsApp client, notify) are injected by the caller — the webhook
wiring happens in M9; tests drive these functions directly with fakes.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import (
    ConversationState,
    Invitation,
    InvitationStatus,
    Language,
    Message,
    MessageDirection,
    MessageType,
    Rsvp,
)
from app.parser import Intent, ParseError, ReplyParser
from app.whatsapp import WhatsAppClient

logger = logging.getLogger(__name__)

Notifier = Callable[[str], None]

MIN_PARTY_SIZE = 1
MAX_PARTY_SIZE = 20

FOLLOW_UP_PROMPTS = {
    Language.en: "Wonderful! How many of you will be coming? Any dietary needs? Anything else we should know?",
    Language.he: "איזה כיף! כמה תהיו? יש העדפות תזונתיות? עוד משהו שכדאי שנדע?",
}

# Final acknowledgement sent to the guest when the RSVP is complete (entering `done`), so a
# guest always hears back — even a one-shot yes-with-count or a decline, which otherwise end
# silently. ``{n}`` is the confirmed head-count (guaranteed non-null when attending is done).
CONFIRM_ATTENDING_PROMPTS = {
    Language.en: "You're all set — we've got you down for {n}. Can't wait to celebrate with you! 🎉",
    Language.he: "הכול מסודר — רשמנו {n}. מתרגשים לחגוג איתכם! 🎉",
}
CONFIRM_DECLINED_PROMPTS = {
    Language.en: "Thanks for letting us know — you'll be missed! 🤍",
    Language.he: "תודה על העדכון — נתגעגע אליכם! 🤍",
}

# Template quick-reply buttons, per language (the templates are bilingual — M9).
_YES_WORDS = {"yes", "כן"}
_NO_WORDS = {"no", "לא"}


def _utcnow() -> datetime:
    """Naive UTC now, matching the naive ``DateTime`` columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def handle_button_reply(
    session: Session,
    invitation: Invitation,
    button_text: str,
    *,
    whatsapp: WhatsAppClient,
    notify: Notifier,
) -> None:
    """A tapped template button — deterministic Yes/No, no LLM (PLAN §6 step 2)."""
    normalized = (button_text or "").strip().lower()
    if normalized in _YES_WORDS:
        answer = True
    elif normalized in _NO_WORDS:
        answer = False
    else:
        notify(f"⚠️ {invitation.name} tapped an unrecognized button: {button_text!r}")
        return
    _apply_reply(
        session,
        invitation,
        explicit=answer,
        party_size=None,
        dietary=None,
        note=None,
        whatsapp=whatsapp,
        notify=notify,
    )


def handle_text_reply(
    session: Session,
    invitation: Invitation,
    text: str,
    *,
    parser: ReplyParser,
    whatsapp: WhatsAppClient,
    notify: Notifier,
) -> None:
    """A free-text reply — parse to structure, then route off the closed ``intent``."""
    try:
        parsed = parser.parse(text)
    except ParseError as exc:
        logger.warning("parse failure for %s: %s", invitation.phone, exc)
        notify(
            f"⚠️ Couldn't understand the reply from {invitation.name}: {text!r} — "
            "nothing was changed; please handle it manually."
        )
        return

    if parsed.confidence is not None:  # logged only — never a control-flow gate (Q5)
        logger.info(
            "parser confidence %.2f for %s (%s)", parsed.confidence, invitation.phone, parsed.intent.value
        )

    if parsed.intent in (Intent.question, Intent.other):
        notify(
            f"❓ {invitation.name} wrote: {text!r} — no RSVP change; please reply personally."
        )
        return

    if parsed.intent is Intent.rsvp_yes:
        explicit: bool | None = True
    elif parsed.intent is Intent.rsvp_no:
        explicit = False
    else:  # provide_details / change: flip only if the parser saw an explicit yes/no
        explicit = parsed.attending

    if explicit is None and invitation.rsvp is None:
        # Details with no yes/no ever given (e.g. a bare "3 of us" before answering):
        # ambiguous — touch nothing, let the Host decide (PLAN §6 · Q5).
        notify(
            f"⚠️ {invitation.name} sent details but hasn't answered yes/no yet: {text!r} — "
            "nothing was changed."
        )
        return

    _apply_reply(
        session,
        invitation,
        explicit=explicit,
        party_size=parsed.party_size,
        dietary=parsed.dietary,
        note=parsed.note,
        whatsapp=whatsapp,
        notify=notify,
    )


def _apply_reply(
    session: Session,
    invitation: Invitation,
    *,
    explicit: bool | None,
    party_size: int | None,
    dietary: str | None,
    note: str | None,
    whatsapp: WhatsAppClient,
    notify: Notifier,
) -> None:
    """Apply one reply's effects in a single transaction, then notify the Host.

    Callers guarantee an RSVP exists or ``explicit`` is non-null, so there is always a row
    to write details onto.
    """
    previous_state = invitation.conversation_state

    if explicit is not None:
        _set_attending(session, invitation, explicit)
    rsvp = invitation.rsvp

    if rsvp.attending:
        if party_size is not None:
            if MIN_PARTY_SIZE <= party_size <= MAX_PARTY_SIZE:
                rsvp.party_size = party_size
            else:
                notify(
                    f"⚠️ {invitation.name} gave a party size of {party_size} — out of the "
                    f"sane range ({MIN_PARTY_SIZE}–{MAX_PARTY_SIZE}), not saved."
                )
        if dietary is not None:
            rsvp.dietary = dietary
        if note is not None:
            rsvp.note = note
        invitation.status = InvitationStatus.confirmed
        invitation.conversation_state = (
            ConversationState.done
            if rsvp.party_size is not None
            else ConversationState.awaiting_details
        )
    else:
        # Declined: a head-count must never ride along (DB invariant); the note may.
        if party_size is not None:
            notify(
                f"⚠️ {invitation.name} declined but mentioned a party size of "
                f"{party_size} — not saved (declines carry no head-count)."
            )
        if note is not None:
            rsvp.note = note
        invitation.status = InvitationStatus.declined
        invitation.conversation_state = ConversationState.done

    rsvp.responded_at = _utcnow()

    new_state = invitation.conversation_state
    if (
        new_state is ConversationState.awaiting_details
        and previous_state is not ConversationState.awaiting_details
    ):
        _send_follow_up(session, invitation, whatsapp)
    elif new_state is ConversationState.done and previous_state is not ConversationState.done:
        _send_confirmation(session, invitation, rsvp, whatsapp)

    session.commit()
    notify(_summary(invitation, rsvp))


def _set_attending(session: Session, invitation: Invitation, attending: bool) -> None:
    """Create or flip the RSVP's ``attending``; a flip to No clears ``party_size``."""
    rsvp = invitation.rsvp
    if rsvp is None:
        rsvp = Rsvp(invitation=invitation, attending=attending, party_size=None)
        session.add(rsvp)
    else:
        rsvp.attending = attending
    if not attending:
        rsvp.party_size = None  # PLAN §6 · Q1: a Yes→No flip clears the head-count


def _send_follow_up(session: Session, invitation: Invitation, whatsapp: WhatsAppClient) -> None:
    """Ask for the head-count/details and log the outbound message to the audit log.

    Free text is allowed here: the guest's own inbound just reopened the 24h window.
    """
    body = FOLLOW_UP_PROMPTS[invitation.language]
    result = whatsapp.send_text(invitation.phone, body)
    session.add(
        Message(
            invitation_id=invitation.id,
            direction=MessageDirection.outbound,
            type=MessageType.text,
            body=body,
            wa_message_id=result.wa_message_id,
        )
    )


def _send_confirmation(
    session: Session, invitation: Invitation, rsvp: Rsvp, whatsapp: WhatsAppClient
) -> None:
    """Acknowledge a settled RSVP to the guest and log the outbound message to the audit log.

    Sent on *entering* ``done`` (mirroring the follow-up): a one-shot yes-with-count or a
    decline would otherwise leave the guest with no reply. Free text is allowed here — the
    guest's own inbound just reopened the 24h window.
    """
    if rsvp.attending:
        body = CONFIRM_ATTENDING_PROMPTS[invitation.language].format(n=rsvp.party_size)
    else:
        body = CONFIRM_DECLINED_PROMPTS[invitation.language]
    result = whatsapp.send_text(invitation.phone, body)
    session.add(
        Message(
            invitation_id=invitation.id,
            direction=MessageDirection.outbound,
            type=MessageType.text,
            body=body,
            wa_message_id=result.wa_message_id,
        )
    )


def _summary(invitation: Invitation, rsvp: Rsvp) -> str:
    """The per-reply Host notification (PLAN §6 step 4: every reply/change fires one)."""
    if not rsvp.attending:
        text = f"💬 {invitation.name} declined."
    elif rsvp.party_size is not None:
        text = f"💬 {invitation.name} is coming — {rsvp.party_size} people."
    else:
        text = f"💬 {invitation.name} is coming — party size still unknown."
    if rsvp.dietary:
        text += f" Dietary: {rsvp.dietary}."
    if rsvp.note:
        text += f" Note: {rsvp.note}."
    return text
