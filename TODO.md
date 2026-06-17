# TODO

My personal running list for the WhatsApp RSVP Bot. Quick capture — add, check off,
or delete freely. For the full build plan see [PLAN.md](./PLAN.md); for the milestone
checklist see [STEPS.md](./STEPS.md).

**Legend:** `[ ]` open · `[x]` done · 🧩 code · 🧪 test · 🌐 external/manual · 🎨 UI

---

## Now / next

- [ ] 🐛 Bug: when sending invites/reminders to non-responders, the name "דנה"
      appears (wrong/placeholder name leaking into the message). Fix this.
- [ ] 🧪 Test with a fake image (event header image path in the invite template).
- [ ] 🌐 Manual smoke test: run uvicorn + streamlit + tunnel, register the `/webhook`
      URL, send myself an invite, reply, and watch it flow to the dashboard.
      *(last open item from STEPS.md M9.4)*

## Phase 2 — Guest-initiated Q&A

- [ ] 🧩 Answer guest questions about the event (LLM Q&A over event details).
      *(`intent=question` currently just logs + notifies the host)*

## Phase 3 — Optional / later

- [ ] 🌐 Deploy to a free always-on cloud tier so reminders fire without my laptop on.
- [ ] 🧩 Richer analytics on the dashboard.
- [ ] 🧩 Support multiple events / reusability.

## Ideas / someday

- [ ] 🎨 Add **event location**: capture it in Event setup (maybe an interactive UI
      map picker), and give guests a Waze/maps link to navigate to the event.

---

*Created 2026-06-17.*
