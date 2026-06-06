# Build Steps — WhatsApp RSVP Bot

The plan ([PLAN.md](./PLAN.md)) broken into small, ordered, independently-verifiable
steps. Work top-to-bottom — later steps depend on earlier ones. A 🧪 step means: add or
extend pytest coverage before moving on.

**Legend:** 🧩 code · 🧪 test · 🌐 external / manual setup · 🎨 Streamlit UI

Each milestone is shippable on its own. **M1–M7 are pure backend with no live calls**
(WhatsApp + OpenAI are faked/stubbed), so they're fully testable offline; only **M0, M9**
and the template steps need live Meta/OpenAI access.

---

## M0 — Scaffolding & setup

- [ ] 🧩 Create the folder structure (`app/`, `host/`, `data/`, `tests/`) plus
  `requirements.txt`, `.env.example`, and a `.gitignore` covering `.env` and `data/`.
- [ ] 🧩 Pin dependencies in `requirements.txt`: `fastapi`, `uvicorn[standard]`,
  `sqlalchemy`, `pydantic-settings`, `phonenumbers`, `openai`, `apscheduler`,
  `streamlit`, `httpx`, `pytest`.
- [ ] 🧩 Create a virtualenv and install the deps.
- [ ] 🌐 Meta: create the app, add the WhatsApp product, note `PHONE_NUMBER_ID`, the test
  sender number, an access token, and the **App Secret**; register your test recipient
  number(s). *(PLAN §10)*
- [ ] 🌐 Get an OpenAI API key.
- [ ] 🌐 Install a tunnel (named cloudflared tunnel or ngrok reserved domain) for a stable
  webhook URL. *(PLAN §7)*
- [ ] 🧩 `app/config.py` — load settings from env via `pydantic-settings`: WhatsApp token,
  `PHONE_NUMBER_ID`, App Secret, webhook verify token, OpenAI key, DB path, reminder delay
  `N` + max count.

## M1 — Data layer

- [ ] 🧩 `app/db.py` — SQLAlchemy engine + session; enable the SQLite **WAL** pragma.
- [ ] 🧩 `app/models.py` — `Event`, `Invitation`, `Rsvp`, `Message` per PLAN §5:
  - `Invitation.phone` **UNIQUE**; `status` (`draft`/`invited`/`confirmed`/`declined`);
    `conversation_state`; reminder fields.
  - `Rsvp` with **nullable** `party_size` + **CHECK** `attending=false ⇒ party_size IS NULL`.
  - `Message.wa_message_id` **UNIQUE** (idempotency key).
  - `Event` single row (couple names en/he, date, image path).
- [ ] 🧩 Table-creation / init helper.
- [ ] 🧪 Model constraint tests on a temp SQLite: unique phone, unique `wa_message_id`, the
  declined⇒NULL check.

## M2 — Phone handling

- [ ] 🧩 `app/phone.py` — validate + canonicalize to E.164 (region `IL` fallback,
  `+countrycode` overrides); reject invalid input. *(PLAN §7 · Q10)*
- [ ] 🧪 Tests: local IL (`054-…` → `+97254…`), explicit `+1…`, bare `wa_id`
  (`97254…` → `+97254…`), and invalid-input rejection.

## M3 — WhatsApp client (injectable seam)

- [ ] 🧩 `app/whatsapp.py` — a client interface (`send_template`, `send_text`,
  `send_interactive`): a real Graph API implementation (`graph.facebook.com/v21.0`, Bearer
  token) **and** a `FakeWhatsAppClient` that records sends. *(PLAN §9, §12)*
- [ ] 🧪 Tests using the fake (assert "template X sent to +972…").

## M4 — Webhook ingestion

- [ ] 🧩 `app/webhook.py` — `GET /webhook` verify (echo `hub.challenge` when the verify
  token matches).
- [ ] 🧩 `POST /webhook` — **signature gate first**: validate `X-Hub-Signature-256`
  (HMAC-SHA256 of the **raw body** with the App Secret, constant-time compare); drop on
  mismatch. *(PLAN §6/§7 · Q8)*
- [ ] 🧩 Branch the payload: `messages[]` → process, `statuses[]` → log/ignore.
  **Idempotency**: insert the `Message` row first; on `wa_message_id` conflict, ack and
  stop. **Ack `200` fast, then process.** *(PLAN §6 · Q4)*
- [ ] 🧩 Sender matching: normalize `wa_id` → E.164 → look up Invitation; no match → log +
  notify (never auto-create). *(PLAN §6 · Q10)*
- [ ] 🧪 Tests: signature accept/reject, duplicate `wa_message_id` (no double-process),
  status-callback ignored, unknown-number path.

## M5 — Parsing & conversation state machine

- [ ] 🧩 `app/parser.py` — OpenAI structured extraction (tool calling) returning
  `{intent, attending, party_size, dietary, note, confidence}`, `intent` a **closed enum**;
  injectable + a stub for tests. *(PLAN §6 · Q5)*
- [ ] 🧩 `app/conversation.py` — button taps (Yes/No) → status / state / RSVP per the
  **transition table**. *(PLAN §5)*
- [ ] 🧩 Free-text handling: control flow off `intent`; `null` never overwrites; flip
  `attending` only on explicit yes/no; validate `party_size` range; `declined ⇒ party_size
  NULL`; question/other → notify Host. *(PLAN §6 · Q5/Q7)*
- [ ] 🧩 RSVP changes — latest reply wins; a Yes→No flip clears `party_size`. *(PLAN §6 · Q1)*
- [ ] 🧪 Table-driven state-machine tests + parser-stub tests for every rule above.

## M6 — Notifications

- [ ] 🧩 `app/notify.py` — append host-facing events to the activity-feed source (swappable
  seam): replies, RSVP changes, unknown numbers, questions, validation failures.
  *(PLAN §6 · Q2)*
- [ ] 🧪 Tests: each event type produces a feed entry.

## M7 — Reminders

- [ ] 🧩 `app/reminders.py` — APScheduler job: find `invited` + `invited_at` older than `N`
  + `reminder_count < max` + **before `event_date`**; re-send the template; increment the
  count. *(PLAN §6 · Q3/Q6)*
- [ ] 🧪 Logic tests with a fake client + injected clock (eligible vs not, cutoff after the
  event date, max-count stop).

## M8 — Streamlit app

- [ ] 🎨 **Event setup** page: couple names (en/he), date, optional image upload → the
  single `event` row.
- [ ] 🎨 **Invitation CRUD**: add/edit with validated phone entry + language; "invited
  twice" duplicate guard.
- [ ] 🎨 **Actions**: Send invites (`draft`→`invited`), Remind non-responders, Nudge for
  details, Re-invite. *(PLAN §8 · Q3/Q11)*
- [ ] 🎨 **Dashboard**: buckets (Coming / Declined / Awaiting reply / Not invited),
  headcount (known heads + count of unknown-size attending), dietary breakdown, activity
  feed, CSV export (unknown size = 1, flagged). *(PLAN §8 · Q11)*

## M9 — Integration, templates & run

- [ ] 🧩 Wire dependency injection: real WhatsApp/OpenAI clients in the app, fakes/stubs in
  tests.
- [ ] 🧪 End-to-end webhook fixture tests through FastAPI `TestClient` (button Yes/No, he/en
  free text, status callback, unknown number, duplicate).
- [ ] 🌐 Create + submit the bilingual **invite** and **reminder** templates (Yes/No
  quick-reply buttons, optional image header) in WhatsApp Manager; wait for approval.
  *(PLAN §6/§7)*
- [ ] 🌐 Manual smoke test: run uvicorn + streamlit + tunnel, register the `/webhook` URL,
  send yourself an invite, reply, and watch it flow to the dashboard.
- [ ] 🧪 *(Optional)* opt-in `llm_eval` suite hitting real OpenAI with ~10 he/en phrases.
