"""ORM models — the tables behind the RSVP bot (PLAN §5, plus the M6 activity feed).

Design notes:

* **Enumerated columns** (`language`, `status`, `conversation_state`, message
  `direction`/`type`) use Python ``enum.Enum`` types mapped through SQLAlchemy's ``Enum``,
  which on SQLite renders as a ``VARCHAR`` plus a ``CHECK`` restricting the column to the
  known values — an invalid state fails loudly instead of being silently stored.
* **Domain invariants live as DB-level constraints**, not just app code: ``Invitation.phone``
  UNIQUE (the natural key), ``Rsvp.invitation_id`` UNIQUE (one RSVP per invitation),
  ``Message.wa_message_id`` UNIQUE (webhook idempotency), the
  ``attending = false ⇒ party_size IS NULL`` CHECK on ``Rsvp``, and a single-row CHECK on
  ``Event``. (These only bite because ``app/db.py`` turns ``PRAGMA foreign_keys=ON``.)
* **No explicit FK from ``Invitation`` to ``Event``:** there is exactly one Event row — the
  implicit parent of every invitation (PLAN §5) — so a join key would be redundant.
* **``Message.invitation_id`` is nullable:** a reply from an unknown number is logged with no
  invitation (PLAN §6), so the audit log must allow orphan rows.
"""

from __future__ import annotations

import enum
from datetime import date, datetime
from urllib.parse import quote

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


# --- Enumerated value sets ---------------------------------------------------------------


class Language(enum.Enum):
    he = "he"
    en = "en"


class InvitationStatus(enum.Enum):
    """The RSVP *outcome* — drives the dashboard buckets and reminder eligibility."""

    draft = "draft"
    invited = "invited"
    confirmed = "confirmed"
    declined = "declined"


class ConversationState(enum.Enum):
    """The *chat router* — what the next inbound message means (orthogonal to status)."""

    none = "none"
    awaiting_yesno = "awaiting_yesno"
    awaiting_details = "awaiting_details"
    done = "done"


class MessageDirection(enum.Enum):
    inbound = "in"
    outbound = "out"


class MessageType(enum.Enum):
    template = "template"
    text = "text"
    interactive = "interactive"
    button = "button"


def _enum_values(enum_cls: type[enum.Enum]) -> list[str]:
    """Persist each enum's ``.value`` (not its member name) in the column."""
    return [member.value for member in enum_cls]


def _enum_type(enum_cls: type[enum.Enum]) -> Enum:
    """A SQLAlchemy ``Enum`` that stores the value and emits a DB-level ``CHECK``.

    ``create_constraint=True`` (off by default in SQLAlchemy 2.0) makes SQLite render a
    ``CHECK (col IN (...))`` so an invalid value is rejected by the database, not just the
    ORM. A fresh instance per call keeps each column's named CHECK distinct.
    """
    return Enum(
        enum_cls,
        values_callable=_enum_values,
        create_constraint=True,
        name=enum_cls.__name__.lower(),
    )


# --- Tables ------------------------------------------------------------------------------


class Event(Base):
    """The single event row — couple names, date, optional header image + location (PLAN §5).

    ``CHECK (id = 1)`` enforces "exactly one Event per deployment" at the database level:
    the autoincrement PK hands the second insert ``id = 2``, which the check rejects.

    **Location** is all-optional: a free-text ``location_name`` (venue/address) and an optional
    precise ``location_lat``/``location_lng`` pair. The map deep-links prefer the coordinates
    when present (most accurate for navigation) and fall back to the address text otherwise, so
    the Host can give *either* and guests still get a working Waze/Google Maps link.
    """

    __tablename__ = "events"
    __table_args__ = (CheckConstraint("id = 1", name="event_single_row"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    partner1_first_en: Mapped[str] = mapped_column(String, nullable=False)
    partner1_last_en: Mapped[str] = mapped_column(String, nullable=False)
    partner2_first_en: Mapped[str] = mapped_column(String, nullable=False)
    partner2_last_en: Mapped[str] = mapped_column(String, nullable=False)
    partner1_first_he: Mapped[str] = mapped_column(String, nullable=False)
    partner1_last_he: Mapped[str] = mapped_column(String, nullable=False)
    partner2_first_he: Mapped[str] = mapped_column(String, nullable=False)
    partner2_last_he: Mapped[str] = mapped_column(String, nullable=False)
    event_date: Mapped[date] = mapped_column(Date, nullable=False)
    image_path: Mapped[str | None] = mapped_column(String, nullable=True)
    location_name: Mapped[str | None] = mapped_column(String, nullable=True)
    location_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    location_lng: Mapped[float | None] = mapped_column(Float, nullable=True)

    @property
    def couple_name_en(self) -> str:
        """Display name, e.g. ``"Ada Cohen & Bo Levi"``."""
        return (
            f"{self.partner1_first_en} {self.partner1_last_en}"
            f" & {self.partner2_first_en} {self.partner2_last_en}"
        )

    @property
    def couple_name_he(self) -> str:
        """Display name with the Hebrew conjunction, e.g. ``"עדה כהן ובו לוי"``."""
        return (
            f"{self.partner1_first_he} {self.partner1_last_he}"
            f" ו{self.partner2_first_he} {self.partner2_last_he}"
        )

    @property
    def has_coordinates(self) -> bool:
        """True when a precise lat/lng pair is set (both, not one)."""
        return self.location_lat is not None and self.location_lng is not None

    @property
    def has_location(self) -> bool:
        """True when there is *anything* to navigate to — coordinates or an address."""
        return self.has_coordinates or bool(self.location_name)

    @property
    def waze_url(self) -> str | None:
        """A Waze deep link, or ``None`` if no location is set.

        Prefers ``ll=`` with ``navigate=yes`` (starts routing to exact coordinates); falls
        back to a ``q=`` address search when only the venue text is known.
        """
        if self.has_coordinates:
            return f"https://waze.com/ul?ll={self.location_lat},{self.location_lng}&navigate=yes"
        if self.location_name:
            return f"https://waze.com/ul?q={quote(self.location_name)}"
        return None

    @property
    def google_maps_url(self) -> str | None:
        """A Google Maps search link, or ``None`` if no location is set.

        Uses the coordinates as the query when available (so the pin lands exactly), else the
        address text — the Maps URL API resolves both the same way.
        """
        if self.has_coordinates:
            query = f"{self.location_lat},{self.location_lng}"
        elif self.location_name:
            query = self.location_name
        else:
            return None
        return f"https://www.google.com/maps/search/?api=1&query={quote(query)}"


class Invitation(Base):
    """A guest invitation — the natural key is the E.164 ``phone`` (PLAN §5).

    ``status`` (the outcome) and ``conversation_state`` (the chat router) are orthogonal and
    written together in one transaction by the conversation layer (M5).
    """

    __tablename__ = "invitations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    phone: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    language: Mapped[Language] = mapped_column(_enum_type(Language), nullable=False)
    status: Mapped[InvitationStatus] = mapped_column(
        _enum_type(InvitationStatus),
        default=InvitationStatus.draft,
        nullable=False,
    )
    conversation_state: Mapped[ConversationState] = mapped_column(
        _enum_type(ConversationState),
        default=ConversationState.none,
        nullable=False,
    )
    reminder_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_reminded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    invited_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    rsvp: Mapped[Rsvp | None] = relationship(
        back_populates="invitation", uselist=False, cascade="all, delete-orphan"
    )
    messages: Mapped[list[Message]] = relationship(
        back_populates="invitation", cascade="all, delete-orphan"
    )


class Rsvp(Base):
    """The RSVP result — one per Invitation (PLAN §5).

    ``party_size`` is nullable: NULL means *attending but size unknown* (distinct from 0).
    The ``attending = false ⇒ party_size IS NULL`` CHECK guarantees a decline never carries a
    headcount, so ``SUM(party_size)`` stays correct even without an ``attending = true``
    filter.
    """

    __tablename__ = "rsvps"
    __table_args__ = (
        CheckConstraint(
            "attending = 1 OR party_size IS NULL", name="rsvp_declined_no_party_size"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invitation_id: Mapped[int] = mapped_column(
        ForeignKey("invitations.id"), unique=True, nullable=False
    )
    attending: Mapped[bool] = mapped_column(Boolean, nullable=False)
    party_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dietary: Mapped[str | None] = mapped_column(Text, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    responded_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    invitation: Mapped[Invitation] = relationship(back_populates="rsvp")


class Message(Base):
    """Append-only audit log of every inbound/outbound WhatsApp message (PLAN §5).

    ``wa_message_id`` is UNIQUE — the webhook idempotency key: a re-delivered Meta event hits
    this constraint and is skipped. It is nullable so locally-originated rows without a Meta
    id don't collide (SQLite treats NULLs as distinct, so multiple are allowed).
    """

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invitation_id: Mapped[int | None] = mapped_column(
        ForeignKey("invitations.id"), nullable=True
    )
    direction: Mapped[MessageDirection] = mapped_column(
        _enum_type(MessageDirection), nullable=False
    )
    type: Mapped[MessageType] = mapped_column(_enum_type(MessageType), nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    wa_message_id: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    raw_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    invitation: Mapped[Invitation | None] = relationship(back_populates="messages")


class HostNotification(Base):
    """One activity-feed entry for the Host (PLAN §6 · Q2 — the M6 feed source).

    Appended by :class:`app.notify.FeedNotifier` (replies, RSVP changes, unknown numbers,
    questions, validation failures) and read newest-first by the Streamlit dashboard. It
    lives in the shared SQLite so the FastAPI process writes and the Streamlit process
    reads. Append-only, like ``messages``; the human-readable ``text`` *is* the event.
    """

    __tablename__ = "host_notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
