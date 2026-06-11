"""Production wiring — the one place real dependencies are constructed (M9 step 1).

Everything else in ``app/`` takes its collaborators as arguments; this module is where
the graph gets built for real: settings from the environment, the process-wide SQLite
engine, the Graph API WhatsApp client, the OpenAI parser, the activity-feed notifier,
and the hourly reminder scheduler (started with the app, shut down with it).

Run with::

    uvicorn app.main:create_app --factory --port 8000

(``--factory`` because constructing the app reads settings and touches the filesystem —
keeping that out of import time is what lets the offline test suite import everything
without a single secret set.)
"""

from __future__ import annotations

from fastapi import FastAPI

from app.config import get_settings
from app.db import get_sessionmaker, init_db
from app.notify import FeedNotifier
from app.parser import build_reply_parser
from app.reminders import create_reminder_scheduler
from app.webhook import create_webhook_app
from app.whatsapp import build_whatsapp_client


def create_app() -> FastAPI:
    """Build the fully-wired FastAPI app: webhook + conversation engine + reminder job."""
    settings = get_settings()
    init_db()
    session_factory = get_sessionmaker()

    whatsapp = build_whatsapp_client(settings)
    parser = build_reply_parser(settings)
    notify = FeedNotifier(session_factory)

    app = create_webhook_app(
        verify_token=settings.webhook_verify_token,
        app_secret=settings.whatsapp_app_secret,
        session_factory=session_factory,
        notify=notify,
        whatsapp=whatsapp,
        parser=parser,
    )

    scheduler = create_reminder_scheduler(
        session_factory,
        whatsapp,
        delay_days=settings.reminder_delay_days,
        max_count=settings.reminder_max_count,
    )
    app.add_event_handler("startup", scheduler.start)
    app.add_event_handler("shutdown", scheduler.shutdown)
    return app
