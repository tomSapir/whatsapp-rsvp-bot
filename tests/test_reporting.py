"""M8 reporting tests — buckets, headcount, dietary, CSV (PLAN §8 · Q11).

The headcount rule gets the closest scrutiny: an attending guest with no size must show
up in ``unknown_size_count`` (never coerced to 0/1 in the metrics) and must export to CSV
as ``1`` with the ``size_unknown`` flag set — visible, not silent.
"""

from __future__ import annotations

import csv
import io

import pytest
from sqlalchemy.orm import Session

from app.db import create_db_engine, init_db
from app.models import (
    ConversationState,
    Invitation,
    InvitationStatus,
    Language,
    Rsvp,
)
from app.reporting import (
    bucket_counts,
    dietary_breakdown,
    export_csv,
    headcount,
)


@pytest.fixture
def session(tmp_path):
    engine = create_db_engine(f"sqlite:///{tmp_path / 'test.sqlite3'}")
    init_db(engine)
    with Session(engine) as s:
        yield s
    engine.dispose()


def _guest(
    session,
    name: str,
    phone: str,
    status: InvitationStatus,
    *,
    attending: bool | None = None,
    party_size: int | None = None,
    dietary: str | None = None,
    note: str | None = None,
) -> Invitation:
    invitation = Invitation(
        name=name,
        phone=phone,
        language=Language.en,
        status=status,
        conversation_state=ConversationState.done
        if status in (InvitationStatus.confirmed, InvitationStatus.declined)
        else ConversationState.none,
    )
    session.add(invitation)
    if attending is not None:
        session.add(
            Rsvp(
                invitation=invitation,
                attending=attending,
                party_size=party_size,
                dietary=dietary,
                note=note,
            )
        )
    session.commit()
    return invitation


@pytest.fixture
def populated(session):
    """2 coming (one size unknown), 1 declined, 1 awaiting, 1 draft."""
    _guest(session, "Ada", "+972502345671", InvitationStatus.confirmed,
           attending=True, party_size=4, dietary="2 vegan")
    _guest(session, "Bo", "+972502345672", InvitationStatus.confirmed,
           attending=True, party_size=None)  # tapped Yes, never gave a number
    _guest(session, "Cy", "+972502345673", InvitationStatus.declined,
           attending=False, note="abroad")
    _guest(session, "Di", "+972502345674", InvitationStatus.invited)
    _guest(session, "Ed", "+972502345675", InvitationStatus.draft)
    return session


def test_bucket_counts(populated):
    buckets = bucket_counts(populated)
    assert buckets.coming == 2
    assert buckets.declined == 1
    assert buckets.awaiting_reply == 1
    assert buckets.not_invited == 1


def test_headcount_reports_floor_and_uncertainty_separately(populated):
    heads = headcount(populated)
    assert heads.known_heads == 4  # Bo is NOT silently counted as 0 or 1
    assert heads.unknown_size_count == 1


def test_dietary_breakdown_attending_only(populated):
    assert dietary_breakdown(populated) == [("Ada", "2 vegan")]


def test_empty_db_reports_zeros(session):
    assert bucket_counts(session).coming == 0
    assert headcount(session) == headcount(session)
    assert headcount(session).known_heads == 0
    assert dietary_breakdown(session) == []
    rows = list(csv.DictReader(io.StringIO(export_csv(session))))
    assert rows == []


def test_csv_exports_unknown_size_as_one_and_flags_it(populated):
    rows = {row["name"]: row for row in csv.DictReader(io.StringIO(export_csv(populated)))}
    assert len(rows) == 5

    assert rows["Ada"]["party_size"] == "4"
    assert rows["Ada"]["size_unknown"] == ""
    assert rows["Ada"]["dietary"] == "2 vegan"

    assert rows["Bo"]["party_size"] == "1"  # substituted for the spreadsheet…
    assert rows["Bo"]["size_unknown"] == "yes"  # …but flagged, never silent

    assert rows["Cy"]["attending"] == "false"
    assert rows["Cy"]["party_size"] == ""  # a decline never carries a head-count
    assert rows["Cy"]["note"] == "abroad"

    assert rows["Di"]["attending"] == ""  # no RSVP yet
    assert rows["Ed"]["status"] == "draft"


def test_csv_export_neutralizes_formula_injection(session):
    """Guest free text that looks like a spreadsheet formula is defanged with a leading '
    (OWASP CSV Injection); phone (E.164 '+') is likewise forced to text, not a number."""
    _guest(
        session,
        "Mallory",
        "+972502345670",
        InvitationStatus.confirmed,
        attending=True,
        party_size=2,
        dietary="=HYPERLINK('https://evil.tld','free food')",
        note="-1 then +1",
    )
    row = {r["name"]: r for r in csv.DictReader(io.StringIO(export_csv(session)))}["Mallory"]
    assert row["dietary"].startswith("'=")  # formula neutralized, not executed
    assert row["note"].startswith("'-")
    assert row["phone"] == "'+972502345670"  # stays text, Excel won't read it as a number
