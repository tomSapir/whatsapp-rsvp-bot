"""Dashboard read-side: buckets, headcount, dietary breakdown, CSV export (PLAN §8 · Q11).

Pure queries — no sends, no writes — so the Streamlit dashboard stays a rendering layer
and these stay unit-testable on a temp SQLite.

The one rule with teeth here is the **headcount**: an attending guest with an unknown
``party_size`` (tapped Yes, never sent a number) is *never silently coerced to 0 or 1*.
The dashboard reports ``known_heads`` (the sum of known sizes) and ``unknown_size_count``
(how many attending invitations haven't said) side by side, so the Host always sees both
the floor and the uncertainty. Only the **CSV export** substitutes ``1`` for an unknown
size — a spreadsheet needs a number — and flags every such row so the substitution is
visible, not silent.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.models import Invitation, InvitationStatus, Rsvp


@dataclass(frozen=True)
class Buckets:
    """Invitation counts by outcome (PLAN §8): the four dashboard tiles."""

    coming: int  # confirmed
    declined: int
    awaiting_reply: int  # invited
    not_invited: int  # draft


@dataclass(frozen=True)
class Headcount:
    """known_heads = SUM of known sizes; unknown_size_count = attending, size not given."""

    known_heads: int
    unknown_size_count: int


def bucket_counts(session: Session) -> Buckets:
    counts = dict(
        session.execute(
            select(Invitation.status, func.count()).group_by(Invitation.status)
        ).all()
    )
    return Buckets(
        coming=counts.get(InvitationStatus.confirmed, 0),
        declined=counts.get(InvitationStatus.declined, 0),
        awaiting_reply=counts.get(InvitationStatus.invited, 0),
        not_invited=counts.get(InvitationStatus.draft, 0),
    )


def headcount(session: Session) -> Headcount:
    known = session.execute(
        select(func.coalesce(func.sum(Rsvp.party_size), 0)).where(Rsvp.attending.is_(True))
    ).scalar_one()
    unknown = session.execute(
        select(func.count())
        .select_from(Rsvp)
        .where(Rsvp.attending.is_(True), Rsvp.party_size.is_(None))
    ).scalar_one()
    return Headcount(known_heads=known, unknown_size_count=unknown)


def dietary_breakdown(session: Session) -> list[tuple[str, str]]:
    """``(guest name, dietary text)`` for every attending guest who reported one."""
    rows = session.execute(
        select(Invitation.name, Rsvp.dietary)
        .join(Rsvp, Rsvp.invitation_id == Invitation.id)
        .where(Rsvp.attending.is_(True), Rsvp.dietary.is_not(None))
        .order_by(Invitation.name)
    ).all()
    return [(name, dietary) for name, dietary in rows]


def guest_list(session: Session) -> list[Invitation]:
    """All invitations with their RSVPs eagerly loaded, for the dashboard table."""
    return list(
        session.execute(
            select(Invitation)
            .options(joinedload(Invitation.rsvp))
            .order_by(Invitation.name)
        )
        .scalars()
        .unique()
    )


CSV_COLUMNS = [
    "name",
    "phone",
    "language",
    "status",
    "attending",
    "party_size",
    "size_unknown",
    "dietary",
    "note",
]


def export_csv(session: Session) -> str:
    """The guest list as CSV text; an unknown size exports as ``1`` with the flag set."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for invitation in guest_list(session):
        rsvp = invitation.rsvp
        attending = rsvp.attending if rsvp else None
        size_unknown = bool(rsvp and rsvp.attending and rsvp.party_size is None)
        writer.writerow(
            {
                "name": invitation.name,
                "phone": invitation.phone,
                "language": invitation.language.value,
                "status": invitation.status.value,
                "attending": "" if attending is None else str(attending).lower(),
                "party_size": (1 if size_unknown else rsvp.party_size or "") if rsvp else "",
                "size_unknown": "yes" if size_unknown else "",
                "dietary": (rsvp.dietary or "") if rsvp else "",
                "note": (rsvp.note or "") if rsvp else "",
            }
        )
    return buffer.getvalue()
