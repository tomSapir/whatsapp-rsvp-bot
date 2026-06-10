"""Host notifications — the activity-feed seam (PLAN §6 · Q2).

"Notify the Host" means **append to the live activity feed** shown in the Streamlit
dashboard — not a WhatsApp message (per-reply WhatsApp push to the Host would hit the same
24h-window/template wall the guests do). The feed surfaces every reply, RSVP change,
unknown-number message, question, and validation failure, newest first.

The seam stays thin on purpose: the webhook (M4) and conversation engine (M5) already take
``notify`` as a plain ``Callable[[str], None]``, so :class:`FeedNotifier` is simply a
*callable object* that appends a :class:`~app.models.HostNotification` row. Swapping in a
free push channel later (ntfy/Telegram — optional Phase 1.5) means writing another
:class:`HostNotifier` and changing only the M9 wiring — never the call sites.

The feed rows live in the shared SQLite, so the FastAPI process writes them and the
Streamlit process reads them (the WAL pragma from M1 makes that concurrent access safe).
Each append uses its own short session and commits immediately — a notification must not
sit inside (or be undone by) the caller's transaction.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import HostNotification


class HostNotifier(ABC):
    """The notification contract — anything callable with the event text.

    Matches the ``Notifier = Callable[[str], None]`` type the M4/M5 call sites depend on,
    so an instance drops straight into ``create_webhook_router(notify=...)`` and
    ``handle_*_reply(notify=...)``.
    """

    @abstractmethod
    def __call__(self, text: str) -> None:
        """Deliver one host-facing event."""


class FeedNotifier(HostNotifier):
    """The v1 channel: append the event to the dashboard activity feed (a DB row)."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def __call__(self, text: str) -> None:
        with self._session_factory() as session:
            session.add(HostNotification(text=text))
            session.commit()


def recent_notifications(session: Session, limit: int = 50) -> list[HostNotification]:
    """The feed, newest first (ordered by id — ``created_at`` only has second precision)."""
    return list(
        session.execute(
            select(HostNotification).order_by(HostNotification.id.desc()).limit(limit)
        ).scalars()
    )
