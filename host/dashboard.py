"""Streamlit Host UI — event setup, guest CRUD, actions, dashboard (PLAN §8, M8; M11 redesign).

Run with ``streamlit run host/dashboard.py``. This file is deliberately a *rendering
layer*: every button delegates to :mod:`app.actions` (writes/sends) and every number on
screen comes from :mod:`app.reporting` (read-only queries) — both fully covered by the
offline test suite. The real Graph API client and the process-wide engine are built once
per Streamlit process via ``st.cache_resource``.

Reads and writes go to the same SQLite the FastAPI engine uses; the WAL pragma (M1) is
what lets the two processes share it safely.

**Look & feel (M11):** a "modern minimal" theme — clean white, a muted sage accent, and
sans-serif type. The palette lives in ``.streamlit/config.toml`` (it colours every
widget); this file adds typography, card/metric polish, a branded header with a live
countdown, Altair charts, and a filterable guest table on top. None of that touches the
data layer — the dashboard still reads :mod:`app.reporting` and writes via
:mod:`app.actions` exactly as before.
"""

from __future__ import annotations

import html
import sys
from datetime import date
from pathlib import Path

# Streamlit puts this file's directory (host/) on sys.path, not the repo root, so the
# `app` package next door is invisible without this.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import altair as alt
import pandas as pd
import streamlit as st

from app import actions, reporting
from app.db import get_sessionmaker, init_db
from app.models import Event, Invitation, InvitationStatus, Language
from app.notify import recent_notifications
from app.phone import InvalidPhoneNumber
from app.whatsapp import build_whatsapp_client

UPLOAD_DIR = Path("data/uploads")

# One place to map an invitation outcome to its dot + label + chart colour, so the tiles,
# the table badges, and the donut all stay in sync.
STATUS_META: dict[InvitationStatus, tuple[str, str, str]] = {
    InvitationStatus.confirmed: ("🟢", "Confirmed", "#4F7A66"),  # sage
    InvitationStatus.declined: ("🔴", "Declined", "#C2705B"),  # muted terracotta
    InvitationStatus.invited: ("🟡", "Awaiting", "#D9A441"),  # warm sand
    InvitationStatus.draft: ("⚪", "Not invited", "#B8BEB9"),  # quiet grey
}


@st.cache_resource
def _resources():
    """Process-wide singletons: schema-initialized sessionmaker + real WhatsApp client."""
    init_db()
    return get_sessionmaker(), build_whatsapp_client()


def _flash_errors(fn, *args, **kwargs):
    """Run an action; surface domain errors inline instead of a stack trace."""
    try:
        return fn(*args, **kwargs)
    except InvalidPhoneNumber as exc:
        st.error(f"Invalid phone number: {exc.raw!r} — enter a local 05x number or +countrycode.")
    except actions.DuplicatePhoneError as exc:
        st.error(f"Already invited: {exc.phone} is taken by another guest.")
    except ValueError as exc:
        st.error(str(exc))
    return None


# --- Look & feel --------------------------------------------------------------------------

# Plain string (not an f-string) — CSS is full of braces. Colours mirror config.toml.
_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

html, body, [data-testid="stAppViewContainer"], [data-testid="stSidebar"] {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
}
.block-container { padding-top: 2.2rem; padding-bottom: 4rem; max-width: 1180px; }

/* Branded header */
.hero { padding: 0.2rem 0 0.9rem; border-bottom: 1px solid #ECEFEC; margin-bottom: 1.4rem; }
.hero-eyebrow { font-size: 0.72rem; font-weight: 600; letter-spacing: 0.14em;
    text-transform: uppercase; color: #8A968E; }
.hero-names { font-size: 1.95rem; font-weight: 700; line-height: 1.15; color: #1F2A24;
    margin-top: 0.15rem; letter-spacing: -0.01em; }
.hero-he { font-size: 1.05rem; color: #6B7771; margin-top: 0.1rem; }
.hero-meta { color: #6B7771; font-size: 0.95rem; margin-top: 0.35rem; }
.hero-pill { display: inline-block; background: #EAF1ED; color: #3F6553; font-weight: 600;
    border-radius: 999px; padding: 0.18rem 0.7rem; font-size: 0.85rem; margin-left: 0.4rem; }

/* Metric tiles → cards */
[data-testid="stMetric"] { background: #FFFFFF; border: 1px solid #E7EBE8;
    border-radius: 14px; padding: 1rem 1.15rem;
    box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04); }
[data-testid="stMetricLabel"] { opacity: 0.72; font-weight: 500; }
[data-testid="stMetricValue"] { font-weight: 700; letter-spacing: -0.02em; }

/* Buttons */
.stButton > button, .stDownloadButton > button {
    border-radius: 10px; font-weight: 600; transition: filter 0.15s ease; }
.stButton > button:hover, .stDownloadButton > button:hover { filter: brightness(0.97); }

/* Tabs */
[data-baseweb="tab-list"] { gap: 0.4rem; }
button[data-baseweb="tab"] { font-weight: 600; }

/* Headings */
h1, h2, h3 { letter-spacing: -0.01em; }

/* Activity-feed timeline */
.feed-row { padding: 0.4rem 0; border-bottom: 1px solid #F0F2F0; font-size: 0.92rem;
    color: #2B352F; }
.feed-row:last-child { border-bottom: none; }
.feed-time { color: #9AA39C; font-size: 0.78rem; font-weight: 600; margin-right: 0.5rem; }
</style>
"""


def _days_to_event(event_date: date) -> int:
    return (event_date - date.today()).days


def _countdown_label(event_date: date) -> str:
    days = _days_to_event(event_date)
    if days > 1:
        return f"{days} days to go"
    if days == 1:
        return "Tomorrow 🎉"
    if days == 0:
        return "Today 🎉"
    return f"{-days} days ago"


def _render_header(event: Event | None) -> None:
    """The branded strip above the tabs: couple, date, live countdown."""
    if event is None:
        st.markdown(
            '<div class="hero"><div class="hero-eyebrow">Wedding RSVP</div>'
            '<div class="hero-names">Welcome 👋</div>'
            '<div class="hero-meta">No event yet — open <b>Event setup</b> to add the '
            "couple and date.</div></div>",
            unsafe_allow_html=True,
        )
        return
    st.markdown(
        f'<div class="hero">'
        f'<div class="hero-eyebrow">Wedding RSVP</div>'
        f'<div class="hero-names">{html.escape(event.couple_name_en)}'
        f'<span class="hero-pill">{_countdown_label(event.event_date)}</span></div>'
        f'<div class="hero-he" dir="rtl">{html.escape(event.couple_name_he)}</div>'
        f'<div class="hero-meta">📅 {event.event_date:%A, %d %B %Y}</div>'
        f"</div>",
        unsafe_allow_html=True,
    )


# --- Dashboard widgets --------------------------------------------------------------------


def _rsvp_donut(buckets: reporting.Buckets) -> alt.Chart:
    """A minimal sage-palette donut of the four RSVP buckets."""
    rows = [
        (STATUS_META[status][1], count, STATUS_META[status][2])
        for status, count in (
            (InvitationStatus.confirmed, buckets.coming),
            (InvitationStatus.invited, buckets.awaiting_reply),
            (InvitationStatus.declined, buckets.declined),
            (InvitationStatus.draft, buckets.not_invited),
        )
    ]
    df = pd.DataFrame(rows, columns=["Status", "Count", "Color"])
    return (
        alt.Chart(df)
        .mark_arc(innerRadius=62, cornerRadius=3, stroke="#FFFFFF", strokeWidth=2)
        .encode(
            theta=alt.Theta("Count:Q", stack=True),
            color=alt.Color(
                "Status:N",
                scale=alt.Scale(domain=df["Status"].tolist(), range=df["Color"].tolist()),
                legend=alt.Legend(orient="right", title=None, labelFontSize=13),
            ),
            tooltip=["Status:N", "Count:Q"],
        )
        .properties(height=240)
    )


def _render_dashboard(session_factory) -> None:
    with session_factory() as session:
        buckets = reporting.bucket_counts(session)
        heads = reporting.headcount(session)
        dietary = reporting.dietary_breakdown(session)
        feed = recent_notifications(session)

    invited_total = buckets.coming + buckets.declined + buckets.awaiting_reply
    responded = buckets.coming + buckets.declined
    response_rate = responded / invited_total if invited_total else 0.0

    st.markdown("#### RSVP overview")
    tiles = st.columns(4)
    tiles[0].metric("🟢 Coming", buckets.coming)
    tiles[1].metric("🔴 Declined", buckets.declined)
    tiles[2].metric("🟡 Awaiting reply", buckets.awaiting_reply)
    tiles[3].metric("⚪ Not invited yet", buckets.not_invited)

    chart_col, stat_col = st.columns([3, 2], gap="large")
    with chart_col:
        if invited_total + buckets.not_invited > 0:
            st.altair_chart(_rsvp_donut(buckets), width="stretch")
        else:
            st.caption("No guests yet — add some in the **Guests** tab.")
    with stat_col:
        st.metric("Response rate", f"{response_rate:.0%}")
        st.progress(response_rate)
        st.metric("Confirmed heads", heads.known_heads)
        if heads.unknown_size_count:
            st.caption(
                f"⚠️ At least {heads.known_heads} — {heads.unknown_size_count} confirmed "
                "guest(s) haven't given a party size yet."
            )

    st.divider()
    left, right = st.columns(2, gap="large")
    with left:
        st.markdown("#### 🥗 Dietary needs")
        if dietary:
            st.dataframe(
                pd.DataFrame(dietary, columns=["Guest", "Dietary"]),
                hide_index=True,
                width="stretch",
            )
        else:
            st.caption("None reported yet.")
    with right:
        st.markdown("#### 📣 Activity feed")
        if feed:
            rows = "".join(
                f'<div class="feed-row"><span class="feed-time">'
                f"{entry.created_at:%d %b · %H:%M}</span>{html.escape(entry.text)}</div>"
                for entry in feed
            )
            st.markdown(rows, unsafe_allow_html=True)
        else:
            st.caption("Nothing yet — replies will show up here.")


# --- Guests: CRUD + per-guest actions -----------------------------------------------------


def _guest_rows(session) -> list[dict]:
    """Flatten the guest list into plain dicts so we can render after the session closes."""
    rows = []
    for guest in reporting.guest_list(session):
        rsvp = guest.rsvp
        rows.append(
            {
                "id": guest.id,
                "name": guest.name,
                "phone": guest.phone,
                "language": guest.language.value,
                "status": guest.status,
                "attending": None if rsvp is None else rsvp.attending,
                "party_size": None if rsvp is None else rsvp.party_size,
                "can_nudge": guest.status is InvitationStatus.confirmed
                and (rsvp is None or rsvp.party_size is None),
                "can_reinvite": guest.status
                in (InvitationStatus.declined, InvitationStatus.draft),
            }
        )
    return rows


def _answer_text(row: dict) -> str:
    if row["attending"] is None:
        return "—"
    if row["attending"]:
        return f"Coming · {row['party_size']}" if row["party_size"] is not None else "Coming · size?"
    return "Declined"


def _render_add_guest(session_factory) -> None:
    st.markdown("#### Add a guest")
    with st.form("add_guest", clear_on_submit=True):
        col1, col2, col3 = st.columns([2, 2, 1])
        new_name = col1.text_input("Name")
        new_phone = col2.text_input("Phone (05x-… or +countrycode)")
        new_language = col3.selectbox("Language", [lang.value for lang in Language])
        if st.form_submit_button("➕ Add guest", type="primary") and new_name and new_phone:
            with session_factory() as session:
                added = _flash_errors(
                    actions.add_invitation,
                    session,
                    name=new_name,
                    phone=new_phone,
                    language=Language(new_language),
                )
            if added:
                st.success(f"Added {added.name} ({added.phone}).")


def _render_guest_actions(session_factory, whatsapp, row: dict) -> None:
    """Contextual actions + edit form for the row selected in the table."""
    st.markdown(f"#### {row['name']}")
    st.caption(f"{row['phone']} · {row['language']} · {STATUS_META[row['status']][1]}")

    btns = st.columns(3)
    if row["can_nudge"] and btns[0].button("🔔 Nudge for details", width="stretch"):
        with session_factory() as session:
            guest = session.get(Invitation, row["id"])
            _flash_errors(actions.nudge_for_details, session, whatsapp, guest)
        st.toast(f"Nudged {row['name']} for details.")
    if row["can_reinvite"] and btns[0].button("✉️ Re-invite", width="stretch"):
        with session_factory() as session:
            guest = session.get(Invitation, row["id"])
            _flash_errors(actions.re_invite, session, whatsapp, guest)
        st.toast(f"Re-invited {row['name']}.")
    if btns[2].button("🗑️ Delete", width="stretch"):
        with session_factory() as session:
            guest = session.get(Invitation, row["id"])
            actions.delete_invitation(session, guest)
        st.rerun()

    with st.expander("✏️ Edit guest details"):
        with st.form(f"edit_guest_{row['id']}"):
            edited_name = st.text_input("Name", value=row["name"])
            edited_phone = st.text_input("Phone", value=row["phone"])
            langs = [lang.value for lang in Language]
            edited_language = st.selectbox(
                "Language", langs, index=langs.index(row["language"])
            )
            if st.form_submit_button("Save changes", type="primary"):
                with session_factory() as session:
                    guest = session.get(actions.Invitation, row["id"])
                    saved = _flash_errors(
                        actions.update_invitation,
                        session,
                        guest,
                        name=edited_name,
                        phone=edited_phone,
                        language=Language(edited_language),
                    )
                if saved:
                    st.success("Saved.")
                    st.rerun()


def _render_guests(session_factory, whatsapp) -> None:
    _render_add_guest(session_factory)
    st.divider()

    with session_factory() as session:
        rows = _guest_rows(session)

    st.markdown("#### Guests")
    if not rows:
        st.caption("No guests yet — add your first above.")
        return

    # Search + status filter.
    filter_col, search_col = st.columns([3, 2])
    with filter_col:
        choice = st.segmented_control(
            "Filter",
            ["All", "Confirmed", "Awaiting", "Declined", "Not invited"],
            default="All",
            label_visibility="collapsed",
        )
    with search_col:
        query = st.text_input(
            "Search", placeholder="🔍 Search name or phone", label_visibility="collapsed"
        )

    status_for_label = {label: status for status, (_, label, _) in STATUS_META.items()}
    filtered = rows
    if choice and choice != "All":
        filtered = [r for r in filtered if r["status"] is status_for_label[choice]]
    if query:
        needle = query.strip().lower()
        filtered = [
            r for r in filtered if needle in r["name"].lower() or needle in r["phone"].lower()
        ]

    if not filtered:
        st.caption("No guests match this filter.")
        return

    table = pd.DataFrame(
        {
            "Status": [f"{STATUS_META[r['status']][0]} {STATUS_META[r['status']][1]}" for r in filtered],
            "Guest": [r["name"] for r in filtered],
            "Phone": [r["phone"] for r in filtered],
            "Lang": [r["language"] for r in filtered],
            "Answer": [_answer_text(r) for r in filtered],
        }
    )
    event = st.dataframe(
        table,
        hide_index=True,
        width="stretch",
        on_select="rerun",
        selection_mode="single-row",
        key="guest_table",
        column_config={
            "Status": st.column_config.TextColumn(width="medium"),
            "Guest": st.column_config.TextColumn(width="medium"),
            "Phone": st.column_config.TextColumn(width="medium"),
            "Lang": st.column_config.TextColumn(width="small"),
            "Answer": st.column_config.TextColumn(width="medium"),
        },
    )

    selected = event.selection.rows
    if selected:
        st.divider()
        _render_guest_actions(session_factory, whatsapp, filtered[selected[0]])
    else:
        st.caption("Select a row to nudge, re-invite, edit, or delete that guest.")


# --- Event setup --------------------------------------------------------------------------


def _render_event_setup(session_factory, current: Event | None) -> None:
    st.markdown("#### Event details")
    with st.form("event_form"):
        st.markdown("**Couple names — English**")
        en1, en2 = st.columns(2)
        p1_first_en = en1.text_input(
            "Partner 1 — first name", value=current.partner1_first_en if current else ""
        )
        p1_last_en = en2.text_input(
            "Partner 1 — last name", value=current.partner1_last_en if current else ""
        )
        p2_first_en = en1.text_input(
            "Partner 2 — first name", value=current.partner2_first_en if current else ""
        )
        p2_last_en = en2.text_input(
            "Partner 2 — last name", value=current.partner2_last_en if current else ""
        )

        st.markdown("**Couple names — Hebrew (עברית)**")
        he1, he2 = st.columns(2)
        p1_first_he = he1.text_input(
            "Partner 1 — first name (he)", value=current.partner1_first_he if current else ""
        )
        p1_last_he = he2.text_input(
            "Partner 1 — last name (he)", value=current.partner1_last_he if current else ""
        )
        p2_first_he = he1.text_input(
            "Partner 2 — first name (he)", value=current.partner2_first_he if current else ""
        )
        p2_last_he = he2.text_input(
            "Partner 2 — last name (he)", value=current.partner2_last_he if current else ""
        )

        event_date = st.date_input(
            "Event date", value=current.event_date if current else date.today()
        )
        image = st.file_uploader("Invite image (optional)", type=["png", "jpg", "jpeg"])
        if st.form_submit_button("💾 Save event", type="primary"):
            image_path = current.image_path if current else None
            if image is not None:
                UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
                image_path = str(UPLOAD_DIR / image.name)
                Path(image_path).write_bytes(image.getbuffer())
            with session_factory() as session:
                actions.upsert_event(
                    session,
                    partner1_first_en=p1_first_en,
                    partner1_last_en=p1_last_en,
                    partner2_first_en=p2_first_en,
                    partner2_last_en=p2_last_en,
                    partner1_first_he=p1_first_he,
                    partner1_last_he=p1_last_he,
                    partner2_first_he=p2_first_he,
                    partner2_last_he=p2_last_he,
                    event_date=event_date,
                    image_path=image_path,
                )
            st.success("Event saved.")
            st.rerun()

    if current and current.image_path and Path(current.image_path).exists():
        st.image(current.image_path, caption="Invite header image", width=300)


# --- Sidebar: event summary + global actions ----------------------------------------------


def _render_sidebar(session_factory, whatsapp, current: Event | None) -> None:
    with st.sidebar:
        st.markdown("### 💍 RSVP Bot")
        if current:
            st.caption(current.couple_name_en)
            st.metric("Countdown", _countdown_label(current.event_date))
        else:
            st.caption("No event configured yet.")

        st.divider()
        st.markdown("**Broadcast**")
        if st.button("📨 Send invites to all drafts", width="stretch"):
            with session_factory() as session:
                sent = actions.send_invites(session, whatsapp)
            st.toast(f"Sent {sent} invite(s).")
        if st.button("🔔 Remind non-responders", width="stretch"):
            with session_factory() as session:
                sent = actions.remind_non_responders(session, whatsapp)
            st.toast(f"Reminded {sent} guest(s).")

        st.divider()
        with session_factory() as session:
            csv_text = reporting.export_csv(session)
        st.download_button(
            "⬇️ Export CSV",
            csv_text,
            file_name="rsvps.csv",
            mime="text/csv",
            width="stretch",
        )


# --- Page assembly ------------------------------------------------------------------------

st.set_page_config(page_title="WhatsApp RSVP Bot", page_icon="💍", layout="wide")
st.markdown(_CSS, unsafe_allow_html=True)
session_factory, whatsapp = _resources()

with session_factory() as session:
    current_event = session.query(Event).one_or_none()

_render_header(current_event)
_render_sidebar(session_factory, whatsapp, current_event)

dashboard_tab, guests_tab, event_tab = st.tabs(["📊 Dashboard", "👥 Guests", "💍 Event setup"])

with dashboard_tab:
    _render_dashboard(session_factory)

with guests_tab:
    _render_guests(session_factory, whatsapp)

with event_tab:
    _render_event_setup(session_factory, current_event)
