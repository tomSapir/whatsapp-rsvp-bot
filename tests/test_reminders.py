"""M7 reminder tests — fake WhatsApp client + injected clock (PLAN §6 · Q3/Q6).

``send_due_reminders`` takes ``now`` as an argument, so every test pins the clock exactly
where it wants it: inside/outside the delay window, before/after the event date. No
sleeping, no real scheduler — the APScheduler wiring is covered by one registration test.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.db import create_db_engine, init_db
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
from app.reminders import JOB_ID, create_reminder_scheduler, send_due_reminders
from app.whatsapp import FakeWhatsAppClient

NOW = datetime(2026, 6, 20, 12, 0, 0)
EVENT_DATE = date(2026, 7, 1)
DELAY_DAYS = 3
MAX_COUNT = 2


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


def _add_event(session, event_date: date = EVENT_DATE) -> None:
    session.add(
        Event(couple_name_en="Ada & Bo", couple_name_he="עדה ובו", event_date=event_date)
    )
    session.commit()


def _add_invitation(
    session,
    *,
    phone: str = "+972502345678",
    status: InvitationStatus = InvitationStatus.invited,
    invited_days_ago: int | None = 5,
    reminder_count: int = 0,
    last_reminded_days_ago: int | None = None,
    language: Language = Language.en,
) -> Invitation:
    invitation = Invitation(
        name="Dana",
        phone=phone,
        language=language,
        status=status,
        conversation_state=ConversationState.awaiting_yesno
        if status is InvitationStatus.invited
        else ConversationState.done,
        invited_at=None if invited_days_ago is None else NOW - timedelta(days=invited_days_ago),
        reminder_count=reminder_count,
        last_reminded_at=None
        if last_reminded_days_ago is None
        else NOW - timedelta(days=last_reminded_days_ago),
    )
    session.add(invitation)
    session.commit()
    return invitation


def _send(session, whatsapp, now: datetime = NOW) -> int:
    return send_due_reminders(
        session, whatsapp, now=now, delay_days=DELAY_DAYS, max_count=MAX_COUNT
    )


# --- Eligibility ------------------------------------------------------------------------------


def test_silent_invitation_past_delay_gets_reminder(session, whatsapp):
    _add_event(session)
    invitation = _add_invitation(session, invited_days_ago=5)

    assert _send(session, whatsapp) == 1

    (sent,) = whatsapp.sent
    assert sent.to == invitation.phone
    assert sent.kind == "template"  # outside the 24h window → must be a template
    assert sent.payload["language"] == "en"
    assert invitation.reminder_count == 1
    assert invitation.last_reminded_at == NOW
    assert invitation.status is InvitationStatus.invited  # unchanged, per the table
    assert invitation.conversation_state is ConversationState.awaiting_yesno


def test_recent_invitation_not_yet_due(session, whatsapp):
    _add_event(session)
    _add_invitation(session, invited_days_ago=1)  # inside the 3-day window
    assert _send(session, whatsapp) == 0
    assert whatsapp.sent == []


@pytest.mark.parametrize(
    "status", [InvitationStatus.draft, InvitationStatus.confirmed, InvitationStatus.declined]
)
def test_only_invited_status_is_chased(session, whatsapp, status):
    _add_event(session)
    _add_invitation(
        session,
        status=status,
        invited_days_ago=None if status is InvitationStatus.draft else 10,
    )
    assert _send(session, whatsapp) == 0


def test_max_count_stops_the_chase(session, whatsapp):
    _add_event(session)
    invitation = _add_invitation(
        session, invited_days_ago=30, reminder_count=MAX_COUNT, last_reminded_days_ago=10
    )
    assert _send(session, whatsapp) == 0
    assert invitation.reminder_count == MAX_COUNT


# --- Spacing: the delay is measured from the last touch --------------------------------------


def test_second_reminder_waits_for_delay_after_first(session, whatsapp):
    _add_event(session)
    _add_invitation(
        session, invited_days_ago=10, reminder_count=1, last_reminded_days_ago=1
    )
    assert _send(session, whatsapp) == 0  # reminded yesterday — not due again yet


def test_second_reminder_sent_once_delay_elapses(session, whatsapp):
    _add_event(session)
    invitation = _add_invitation(
        session, invited_days_ago=10, reminder_count=1, last_reminded_days_ago=4
    )
    assert _send(session, whatsapp) == 1
    assert invitation.reminder_count == 2


# --- The event-date cutoff --------------------------------------------------------------------


def test_no_reminders_on_or_after_event_date(session, whatsapp):
    _add_event(session)
    _add_invitation(session, invited_days_ago=10)
    on_the_day = datetime.combine(EVENT_DATE, datetime.min.time())
    assert _send(session, whatsapp, now=on_the_day) == 0
    assert _send(session, whatsapp, now=on_the_day + timedelta(days=3)) == 0
    assert whatsapp.sent == []


def test_no_event_row_means_no_reminders(session, whatsapp):
    _add_invitation(session, invited_days_ago=10)
    assert _send(session, whatsapp) == 0


# --- Side effects of a send -------------------------------------------------------------------


def test_reminder_is_audit_logged(session, whatsapp):
    _add_event(session)
    invitation = _add_invitation(session, invited_days_ago=5)
    _send(session, whatsapp)

    (logged,) = session.query(Message).all()
    assert logged.invitation_id == invitation.id
    assert logged.direction is MessageDirection.outbound
    assert logged.type is MessageType.template
    assert logged.wa_message_id is not None


def test_reminder_uses_invitation_language(session, whatsapp):
    _add_event(session)
    _add_invitation(session, invited_days_ago=5, language=Language.he)
    _send(session, whatsapp)
    assert whatsapp.sent[0].payload["language"] == "he"


def test_mixed_guests_only_due_ones_reminded(session, whatsapp):
    _add_event(session)
    due = _add_invitation(session, phone="+972502345678", invited_days_ago=5)
    _add_invitation(session, phone="+972502345679", invited_days_ago=1)  # too recent
    _add_invitation(
        session, phone="+972502345670", status=InvitationStatus.confirmed
    )  # already replied

    assert _send(session, whatsapp) == 1  # only the silent, overdue one
    (sent,) = whatsapp.sent
    assert sent.to == due.phone


# --- Scheduler wiring -------------------------------------------------------------------------


def test_scheduler_registers_hourly_job(tmp_path, whatsapp):
    engine = create_db_engine(f"sqlite:///{tmp_path / 'sched.sqlite3'}")
    init_db(engine)
    from sqlalchemy.orm import sessionmaker

    scheduler = create_reminder_scheduler(
        sessionmaker(bind=engine, future=True),
        whatsapp,
        delay_days=DELAY_DAYS,
        max_count=MAX_COUNT,
    )
    job = scheduler.get_job(JOB_ID)
    assert job is not None  # registered without being started — M9 calls .start()
    engine.dispose()
