"""Reminders — chase silent invitations on a schedule (PLAN §6 · Q3/Q6).

An invitation is **due** for a reminder when all of these hold:

* ``status = invited`` — the guest has the invite but never answered (a reply flips the
  status to ``confirmed``/``declined``, which ends the chase automatically);
* the invite was actually sent (``invited_at`` is set — ``draft`` rows are never chased);
* ``reminder_count < max`` — the configured nag budget isn't spent;
* at least ``delay_days`` have passed since the **last touch** — ``last_reminded_at`` if a
  reminder was already sent, else ``invited_at``. (Measuring from the last touch, not from
  ``invited_at`` alone, is what spaces consecutive reminders ``N`` days apart instead of
  letting the hourly job fire them back-to-back once the first window opens.)
* **today is still before ``event.event_date``** — the hard cutoff: the event has passed
  (or arrived), the loop stops, no matter how many reminders remain.

A reminder lands outside the 24h session window by definition (the guest has been silent
for days), so it must be a pre-approved **template** (PLAN §6). Each send increments
``reminder_count``, stamps ``last_reminded_at``, re-arms ``conversation_state =
awaiting_yesno`` (per the §5 transition table — ``status`` stays ``invited``), and is
appended to the ``messages`` audit log.

Scope (deliberate, PLAN §6): only *silent* guests are auto-chased. A guest who said Yes
but never gave a head-count (``confirmed`` + ``party_size IS NULL``) is **not** reminded
automatically — the dashboard surfaces those and the Host nudges manually (M8).

Split for testability: :func:`send_due_reminders` is the pure logic — it takes ``now`` as
an argument (the injected clock) plus a session and a WhatsApp client, so tests drive it
with a fake client and hand-picked datetimes. :func:`create_reminder_scheduler` is the
thin APScheduler wiring (an hourly interval job) that M9 starts alongside the API.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import (
    ConversationState,
    Event,
    Invitation,
    InvitationStatus,
    Message,
    MessageDirection,
    MessageType,
)
from app.whatsapp import WhatsAppClient

logger = logging.getLogger(__name__)

DEFAULT_REMINDER_TEMPLATE = "rsvp_reminder"
JOB_ID = "send-due-reminders"


def _utcnow() -> datetime:
    """Naive UTC now, matching the naive ``DateTime`` columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def send_due_reminders(
    session: Session,
    whatsapp: WhatsAppClient,
    *,
    now: datetime,
    delay_days: int,
    max_count: int,
    template_name: str = DEFAULT_REMINDER_TEMPLATE,
) -> int:
    """Re-send the template to every due invitation; return how many were sent.

    ``now`` is injected rather than read from the wall clock so tests can place
    themselves freely before/after the delay window and the event date.
    """
    event = session.execute(select(Event)).scalar_one_or_none()
    if event is None:
        logger.warning("reminders skipped: no Event row configured yet")
        return 0
    if now.date() >= event.event_date:
        logger.info("reminders stopped: event date %s reached", event.event_date)
        return 0

    candidates = (
        session.execute(
            select(Invitation).where(
                Invitation.status == InvitationStatus.invited,
                Invitation.invited_at.is_not(None),
                Invitation.reminder_count < max_count,
            )
        )
        .scalars()
        .all()
    )

    due_before = now - timedelta(days=delay_days)
    sent = 0
    for invitation in candidates:
        last_touch = invitation.last_reminded_at or invitation.invited_at
        if last_touch > due_before:
            continue  # touched too recently — not yet due

        result = whatsapp.send_template(
            invitation.phone, template_name, invitation.language.value
        )
        invitation.reminder_count += 1
        invitation.last_reminded_at = now
        invitation.conversation_state = ConversationState.awaiting_yesno
        session.add(
            Message(
                invitation_id=invitation.id,
                direction=MessageDirection.outbound,
                type=MessageType.template,
                body=template_name,
                wa_message_id=result.wa_message_id,
            )
        )
        sent += 1
        logger.info(
            "reminder %d/%d sent to %s", invitation.reminder_count, max_count, invitation.phone
        )

    session.commit()
    return sent


def create_reminder_scheduler(
    session_factory: sessionmaker[Session],
    whatsapp: WhatsAppClient,
    *,
    delay_days: int,
    max_count: int,
    template_name: str = DEFAULT_REMINDER_TEMPLATE,
    interval_minutes: int = 60,
) -> BackgroundScheduler:
    """Wire :func:`send_due_reminders` into an hourly APScheduler job (started by M9).

    The job opens its own short session per run and reads the real clock — all the logic
    (and all the tests) live in :func:`send_due_reminders`.
    """

    def _run() -> None:
        with session_factory() as session:
            send_due_reminders(
                session,
                whatsapp,
                now=_utcnow(),
                delay_days=delay_days,
                max_count=max_count,
                template_name=template_name,
            )

    scheduler = BackgroundScheduler()
    scheduler.add_job(_run, "interval", minutes=interval_minutes, id=JOB_ID)
    return scheduler
