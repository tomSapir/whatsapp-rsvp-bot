# TODO

My personal running list for the WhatsApp RSVP Bot. Quick capture — add, check off,
or delete freely. For the full build plan see [PLAN.md](./PLAN.md); for the milestone
checklist see [STEPS.md](./STEPS.md).

**Legend:** `[ ]` open · `[x]` done · 🧩 code · 🧪 test · 🌐 external/manual · 🎨 UI

---

## Now / next

- [ ] 📋 Read [CODE_REVIEW.md](./CODE_REVIEW.md) (full deep review, 2026-06-17) and work
      through its findings in a new session. Top item: Host-notify-on-failure wrapper
      (#1/#2) closes the silent-drop gap. *(Note: #3 "template `components`" was a
      misdiagnosis — see the `דנה` item below. The code is correct; the templates are
      deliberately parameter-less.)*
- [⏳] 🐛 **`דנה` bug — WAITING ON META APPROVAL.** Root cause (2026-06-18): the Hebrew
      `rsvp_details_nudge` template body hardcodes "תום ו**דנה**" instead of "תום ו**עמית**"
      — a one-word typo baked into the approved template text, *not* a code bug (the live
      templates have no `{{…}}` variables, so `components=None` is correct). Fix: edited the
      Hebrew nudge body in WhatsApp Manager → resubmitted → **waiting for re-approval**.
      Verify status flips back to APPROVED, then send myself a Hebrew nudge to confirm.
- [ ] 🧪 Test with a fake image (event header image path in the invite template).
- [ ] 🌐 Manual smoke test: run uvicorn + streamlit + tunnel, register the `/webhook`
      URL, send myself an invite, reply, and watch it flow to the dashboard.
      *(last open item from STEPS.md M9.4)*

## Phase 2 — Guest-initiated Q&A

- [ ] 🧩 Answer guest questions about the event (LLM Q&A over event details).
      *(`intent=question` currently just logs + notifies the host)*

## Phase 3 — Optional / later

- [ ] 🌐 Upload the host app to **Streamlit Community Cloud** (share.streamlit.io) — push
      to GitHub, point it at `host/dashboard.py`, set secrets (DB path, WhatsApp token).
      *Caveat:* Streamlit Cloud only runs the dashboard — it won't host the FastAPI
      `/webhook` or the APScheduler reminder job, and it has no built-in auth (CODE_REVIEW
      note: gate it before exposing publicly). The webhook + reminders still need the
      always-on tier below.
- [ ] 🌐 Deploy to a free always-on cloud tier so reminders fire without my laptop on.
- [ ] 🧩 Richer analytics on the dashboard.
- [ ] 🧩 Support multiple events / reusability.

## Ideas / someday

- [x] 🎨 Add **event location**: capture it in Event setup (venue/address + optional
      lat/lng with a map preview), and give guests Waze + Google Maps links on
      confirmation. *(coords-or-address links on the Event model; sent in the attending
      confirmation. Skipped the interactive click-picker to stay dependency-free.)*

---

*Created 2026-06-17.*
