"""Host actions — the write-side operations behind the Streamlit buttons (PLAN §8).

The Streamlit file (``host/dashboard.py``) stays a thin rendering layer; everything a
button *does* lives here, taking an explicit session + WhatsApp client so it is testable
with the M3 fake exactly like the engine code. All sends here are **templates**: each of
these actions targets a guest who has been silent for a while (or never contacted), so
the 24h free-text window cannot be assumed open.

The actions, per PLAN §8 and the §5 transition table:

* :func:`send_invites` — every ``draft`` → send the invite template → ``invited`` +
  ``awaiting_yesno`` + ``invited_at`` stamped.
* :func:`remind_non_responders` — the **manual** "remind now" button: every ``invited``
  guest gets the reminder template immediately. Unlike the M7 auto-job it ignores the
  delay window and the max count — the Host clicked, the Host decides — but it still
  increments ``reminder_count``/``last_reminded_at`` so the auto-job stays honest.
* :func:`nudge_for_details` — per-guest, for *confirmed but incomplete* (Yes, no
  head-count): send the count-prompt template and re-arm ``awaiting_details``.
* :func:`re_invite` — per-guest, manual reset for ``declined``/``draft``: wipe the RSVP,
  back to ``invited`` + ``awaiting_yesno``, re-send the invite template.

Event setup and invitation CRUD live here too: phone numbers are validated +
canonicalized to E.164 **at entry** (:mod:`app.phone` — invalid input is rejected with
:class:`~app.phone.InvalidPhoneNumber`, never stored), and the "invited twice" guard
(:class:`DuplicatePhoneError`) compares canonical forms, so the same number typed two
different ways is still caught.
"""

from __future__ import annotations

import logging
from datetime import date as date_type
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    ConversationState,
    Event,
    Invitation,
    InvitationStatus,
    Language,
    Message,
    MessageDirection,
    MessageType,
)
from app.phone import normalize_phone
from app.reminders import DEFAULT_REMINDER_TEMPLATE
from app.whatsapp import WhatsAppClient

logger = logging.getLogger(__name__)

DEFAULT_INVITE_TEMPLATE = "wedding_invite"
DEFAULT_NUDGE_TEMPLATE = "rsvp_details_nudge"


class DuplicatePhoneError(ValueError):
    """The canonical form of this phone already belongs to another invitation."""

    def __init__(self, phone: str) -> None:
        super().__init__(f"an invitation for {phone} already exists")
        self.phone = phone


def _utcnow() -> datetime:
    """Naive UTC now, matching the naive ``DateTime`` columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _send_template_and_log(
    session: Session, whatsapp: WhatsAppClient, invitation: Invitation, template_name: str
) -> None:
    """Send ``template_name`` in the guest's language and append the audit-log row."""
    result = whatsapp.send_template(
        invitation.phone, template_name, invitation.language.value
    )
    session.add(
        Message(
            invitation_id=invitation.id,
            direction=MessageDirection.outbound,
            type=MessageType.template,
            body=template_name,
            wa_message_id=result.wa_message_id,
        )
    )


# --- Event setup -------------------------------------------------------------------------


def upsert_event(
    session: Session,
    *,
    couple_name_en: str,
    couple_name_he: str,
    event_date: date_type,
    image_path: str | None = None,
) -> Event:
    """Create or update the single Event row (the ``CHECK (id = 1)`` guard stays happy)."""
    event = session.execute(select(Event)).scalar_one_or_none()
    if event is None:
        event = Event(
            couple_name_en=couple_name_en,
            couple_name_he=couple_name_he,
            event_date=event_date,
            image_path=image_path,
        )
        session.add(event)
    else:
        event.couple_name_en = couple_name_en
        event.couple_name_he = couple_name_he
        event.event_date = event_date
        event.image_path = image_path
    session.commit()
    return event


# --- Invitation CRUD ----------------------------------------------------------------------


def add_invitation(session: Session, *, name: str, phone: str, language: Language) -> Invitation:
    """Add a guest; the phone is canonicalized at entry and guarded against duplicates.

    Raises :class:`~app.phone.InvalidPhoneNumber` for unparseable input and
    :class:`DuplicatePhoneError` when the canonical number is already taken.
    """
    canonical = normalize_phone(phone)
    _ensure_phone_free(session, canonical)
    invitation = Invitation(name=name.strip(), phone=canonical, language=language)
    session.add(invitation)
    session.commit()
    return invitation


def update_invitation(
    session: Session,
    invitation: Invitation,
    *,
    name: str | None = None,
    phone: str | None = None,
    language: Language | None = None,
) -> Invitation:
    """Edit a guest; a changed phone is re-canonicalized and re-guarded."""
    if name is not None:
        invitation.name = name.strip()
    if phone is not None:
        canonical = normalize_phone(phone)
        if canonical != invitation.phone:
            _ensure_phone_free(session, canonical)
            invitation.phone = canonical
    if language is not None:
        invitation.language = language
    session.commit()
    return invitation


def delete_invitation(session: Session, invitation: Invitation) -> None:
    """Remove a guest (the RSVP and message rows cascade)."""
    session.delete(invitation)
    session.commit()


def _ensure_phone_free(session: Session, canonical: str) -> None:
    existing = session.execute(
        select(Invitation).where(Invitation.phone == canonical)
    ).scalar_one_or_none()
    if existing is not None:
        raise DuplicatePhoneError(canonical)


# --- Bulk actions ---------------------------------------------------------------------------


def send_invites(
    session: Session,
    whatsapp: WhatsAppClient,
    *,
    now: datetime | None = None,
    template_name: str = DEFAULT_INVITE_TEMPLATE,
) -> int:
    """Send the invite template to every ``draft`` guest; return how many were sent."""
    now = now or _utcnow()
    drafts = (
        session.execute(
            select(Invitation).where(Invitation.status == InvitationStatus.draft)
        )
        .scalars()
        .all()
    )
    for invitation in drafts:
        _send_template_and_log(session, whatsapp, invitation, template_name)
        invitation.status = InvitationStatus.invited
        invitation.conversation_state = ConversationState.awaiting_yesno
        invitation.invited_at = now
    session.commit()
    return len(drafts)


def remind_non_responders(
    session: Session,
    whatsapp: WhatsAppClient,
    *,
    now: datetime | None = None,
    template_name: str = DEFAULT_REMINDER_TEMPLATE,
) -> int:
    """Manually remind every silent guest (``invited``) right now; return how many.

    Deliberately skips the auto-job's delay/max-count guards — an explicit Host click —
    but records the send in the same counters so the auto-job's spacing stays correct.
    """
    now = now or _utcnow()
    silent = (
        session.execute(
            select(Invitation).where(
                Invitation.status == InvitationStatus.invited,
                Invitation.invited_at.is_not(None),
            )
        )
        .scalars()
        .all()
    )
    for invitation in silent:
        _send_template_and_log(session, whatsapp, invitation, template_name)
        invitation.reminder_count += 1
        invitation.last_reminded_at = now
        invitation.conversation_state = ConversationState.awaiting_yesno
    session.commit()
    return len(silent)


# --- Per-guest actions ------------------------------------------------------------------------


def nudge_for_details(
    session: Session,
    whatsapp: WhatsAppClient,
    invitation: Invitation,
    *,
    template_name: str = DEFAULT_NUDGE_TEMPLATE,
) -> None:
    """Chase a *confirmed but incomplete* guest (Yes, no head-count) for their details.

    This is the manual counterpart to the deliberate M7 gap: the auto-reminder never
    chases confirmed guests, the Host does — from the dashboard, per guest.
    """
    if invitation.status is not InvitationStatus.confirmed:
        raise ValueError("nudge is only for confirmed guests")
    if invitation.rsvp is not None and invitation.rsvp.party_size is not None:
        raise ValueError("nothing to nudge: the head-count is already known")
    _send_template_and_log(session, whatsapp, invitation, template_name)
    invitation.conversation_state = ConversationState.awaiting_details
    session.commit()


def re_invite(
    session: Session,
    whatsapp: WhatsAppClient,
    invitation: Invitation,
    *,
    now: datetime | None = None,
    template_name: str = DEFAULT_INVITE_TEMPLATE,
) -> None:
    """Manual reset per the §5 table: RSVP wiped, back to ``invited``/``awaiting_yesno``.

    Only valid from ``declined`` or ``draft`` (PLAN §8) — re-inviting a confirmed guest
    would throw away a real answer.
    """
    if invitation.status not in (InvitationStatus.declined, InvitationStatus.draft):
        raise ValueError("re-invite is only for declined or draft guests")
    if invitation.rsvp is not None:
        session.delete(invitation.rsvp)
    _send_template_and_log(session, whatsapp, invitation, template_name)
    invitation.status = InvitationStatus.invited
    invitation.conversation_state = ConversationState.awaiting_yesno
    invitation.invited_at = now or _utcnow()
    invitation.reminder_count = 0
    invitation.last_reminded_at = None
    session.commit()
