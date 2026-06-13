"""Streamlit Host UI — event setup, guest CRUD, actions, dashboard (PLAN §8, M8).

Run with ``streamlit run host/dashboard.py``. This file is deliberately a *rendering
layer*: every button delegates to :mod:`app.actions` (writes/sends) and every number on
screen comes from :mod:`app.reporting` (read-only queries) — both fully covered by the
offline test suite. The real Graph API client and the process-wide engine are built once
per Streamlit process via ``st.cache_resource``.

Reads and writes go to the same SQLite the FastAPI engine uses; the WAL pragma (M1) is
what lets the two processes share it safely.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

# Streamlit puts this file's directory (host/) on sys.path, not the repo root, so the
# `app` package next door is invisible without this.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st

from app import actions, reporting
from app.db import get_sessionmaker, init_db
from app.models import InvitationStatus, Language
from app.notify import recent_notifications
from app.phone import InvalidPhoneNumber
from app.whatsapp import build_whatsapp_client

UPLOAD_DIR = Path("data/uploads")


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


st.set_page_config(page_title="WhatsApp RSVP Bot", page_icon="💍", layout="wide")
session_factory, whatsapp = _resources()

dashboard_tab, guests_tab, event_tab = st.tabs(["📊 Dashboard", "👥 Guests", "💍 Event setup"])


# --- Event setup ----------------------------------------------------------------------------

with event_tab:
    st.subheader("Event")
    with session_factory() as session:
        from app.models import Event

        current = session.query(Event).one_or_none()

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
        if st.form_submit_button("Save event"):
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


# --- Guests: CRUD + per-guest actions ----------------------------------------------------------

with guests_tab:
    st.subheader("Add a guest")
    with st.form("add_guest", clear_on_submit=True):
        col1, col2, col3 = st.columns([2, 2, 1])
        new_name = col1.text_input("Name")
        new_phone = col2.text_input("Phone (05x-… or +countrycode)")
        new_language = col3.selectbox("Language", [lang.value for lang in Language])
        if st.form_submit_button("Add guest") and new_name and new_phone:
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

    st.divider()
    st.subheader("Guests")

    with session_factory() as session:
        guests = reporting.guest_list(session)

        for guest in guests:
            rsvp = guest.rsvp
            answer = (
                "—"
                if rsvp is None
                else ("Coming" if rsvp.attending else "Declined")
                + (f", {rsvp.party_size}" if rsvp.party_size is not None else ", size?")
            )
            cols = st.columns([2, 2, 1, 1, 1, 1, 1])
            cols[0].write(f"**{guest.name}**")
            cols[1].write(guest.phone)
            cols[2].write(guest.language.value)
            cols[3].write(guest.status.value)
            cols[4].write(answer)

            if guest.status is InvitationStatus.confirmed and (
                rsvp is None or rsvp.party_size is None
            ):
                if cols[5].button("Nudge", key=f"nudge-{guest.id}"):
                    _flash_errors(actions.nudge_for_details, session, whatsapp, guest)
                    st.toast(f"Nudged {guest.name} for details.")
            if guest.status in (InvitationStatus.declined, InvitationStatus.draft):
                if cols[5].button("Re-invite", key=f"reinvite-{guest.id}"):
                    _flash_errors(actions.re_invite, session, whatsapp, guest)
                    st.toast(f"Re-invited {guest.name}.")
            if cols[6].button("Delete", key=f"delete-{guest.id}"):
                actions.delete_invitation(session, guest)
                st.rerun()

        with st.expander("✏️ Edit a guest"):
            if guests:
                target = st.selectbox(
                    "Guest", guests, format_func=lambda g: f"{g.name} ({g.phone})"
                )
                with st.form("edit_guest"):
                    edited_name = st.text_input("Name", value=target.name)
                    edited_phone = st.text_input("Phone", value=target.phone)
                    edited_language = st.selectbox(
                        "Language",
                        [lang.value for lang in Language],
                        index=[lang.value for lang in Language].index(target.language.value),
                    )
                    if st.form_submit_button("Save changes"):
                        if _flash_errors(
                            actions.update_invitation,
                            session,
                            target,
                            name=edited_name,
                            phone=edited_phone,
                            language=Language(edited_language),
                        ):
                            st.success("Saved.")
                            st.rerun()
            else:
                st.caption("No guests yet.")


# --- Dashboard ----------------------------------------------------------------------------------

with dashboard_tab:
    with session_factory() as session:
        buckets = reporting.bucket_counts(session)
        heads = reporting.headcount(session)
        dietary = reporting.dietary_breakdown(session)
        feed = recent_notifications(session)
        csv_text = reporting.export_csv(session)

    st.subheader("RSVPs")
    tiles = st.columns(4)
    tiles[0].metric("✅ Coming", buckets.coming)
    tiles[1].metric("❌ Declined", buckets.declined)
    tiles[2].metric("⏳ Awaiting reply", buckets.awaiting_reply)
    tiles[3].metric("📝 Not invited yet", buckets.not_invited)

    st.subheader("Headcount")
    head_cols = st.columns(2)
    head_cols[0].metric("Known heads", heads.known_heads)
    head_cols[1].metric("Coming, size unknown", heads.unknown_size_count)
    if heads.unknown_size_count:
        st.caption(
            "The real total is at least the known heads — "
            f"{heads.unknown_size_count} attending invitation(s) haven't given a size yet."
        )

    action_cols = st.columns(3)
    if action_cols[0].button("📨 Send invites to all drafts"):
        with session_factory() as session:
            sent = actions.send_invites(session, whatsapp)
        st.toast(f"Sent {sent} invite(s).")
    if action_cols[1].button("🔔 Remind non-responders now"):
        with session_factory() as session:
            sent = actions.remind_non_responders(session, whatsapp)
        st.toast(f"Reminded {sent} guest(s).")
    action_cols[2].download_button(
        "⬇️ Export CSV", csv_text, file_name="rsvps.csv", mime="text/csv"
    )

    left, right = st.columns(2)
    with left:
        st.subheader("Dietary needs")
        if dietary:
            st.table([{"Guest": name, "Dietary": text} for name, text in dietary])
        else:
            st.caption("None reported yet.")
    with right:
        st.subheader("Activity feed")
        if feed:
            for entry in feed:
                st.write(f"`{entry.created_at:%d/%m %H:%M}` {entry.text}")
        else:
            st.caption("Nothing yet — replies will show up here.")
