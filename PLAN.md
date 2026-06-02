# WhatsApp Guest Confirmation (RSVP) Bot — Project Plan

> **RSVP** = *Répondez s'il vous plaît* ("please reply") — the request for invited
> guests to confirm whether they'll attend. This bot collects those confirmations
> over WhatsApp.

## 1. Summary

A **for-fun personal project** (no real event — built for learning/portfolio, $0 to
build and test). A WhatsApp bot that:

1. Lets the host manage a guest list in a small admin UI.
2. Sends each guest a WhatsApp message asking them to confirm attendance.
3. Understands their reply — *attending? / how many / dietary / free-text note* —
   using tappable buttons **plus** an LLM to parse messy free text.
4. Automatically reminds guests who haven't responded.
5. Shows the host a live dashboard, notifies them on each reply, and exports CSV.

Messages are **bilingual** — Hebrew or English chosen per guest.

## 2. Decisions (locked)

| Area | Decision |
|------|----------|
| Use case | Personal, one-time (for-fun) event |
| Flow — v1 | **Bot-initiated outbound** ("Will you attend?") + capture replies |
| Flow — later (Phase 2) | Two-way (guests can ask/change too) |
| WhatsApp integration | **Official WhatsApp Business Cloud API** (Meta) — free at test scale |
| Language | Python 3.12 |
| Backend / engine | **FastAPI** (webhook + sending + scheduler) |
| Admin UI / dashboard | **Streamlit** (separate process, shares the DB) |
| Storage | **SQLite** (WAL mode), via SQLAlchemy |
| Guest list source | Small admin UI (add/edit guests) |
| RSVP fields collected | Attending (yes/no), party size, dietary/meal, free-text note |
| Reply understanding | **Hybrid** — Yes/No via buttons, LLM extracts count/dietary/note |
| LLM provider | **OpenAI** (reuse existing setup) |
| Reminders | **Auto-remind** non-responders after a configurable delay |
| Host visibility | Live dashboard + per-reply notification + CSV export |
| Hosting (v1) | Run locally; expose webhook via a **tunnel** (cloudflared/ngrok) |

## 3. Why the official Cloud API costs $0 here

- Meta provides a **free test sender number** and lets you register **up to 5 recipient
  numbers** (yours + friends) — messaging them is **free, no payment method required**.
- **Inbound replies** and **any free-form message within the 24-hour window** are free.
- Per-message charges only apply to *production* sends to arbitrary numbers at volume
  (marketing-category templates). Not this project.

## 4. Architecture

```
                 ┌────────────────────────┐
                 │      Streamlit admin    │   add/edit guests, send invites,
                 │   (admin/dashboard.py)  │   live dashboard, CSV export
                 └───────────┬─────────────┘
                             │ read/write
                       ┌─────▼──────┐
                       │  SQLite DB │  guests · responses · message log
                       │ (WAL mode) │
                       └─────▲──────┘
                             │ read/write
   guests' phones    ┌───────┴─────────────┐      ┌──────────────┐
   ┌──────────┐      │   FastAPI engine    │─────▶│  OpenAI API  │ parse free-text
   │ WhatsApp │◀────▶│   (app/webhook.py)  │      │  (extract)   │ replies → JSON
   └──────────┘      │  • GET  /webhook    │      └──────────────┘
        ▲            │    (Meta verify)    │
        │            │  • POST /webhook    │
        │            │    (incoming msgs)  │
        │            │  • send invites     │
        │            │  • reminder scheduler│
        │            └──────────┬──────────┘
        │                       │ HTTPS (Graph API)
        │            ┌──────────▼──────────┐
        └────────────│  Meta WhatsApp      │
         (via tunnel)│  Cloud API          │
                     └─────────────────────┘
```

Two processes share one SQLite file:
- **FastAPI engine** — receives the webhook, sends messages, runs the reminder job.
  Exposed to the internet via the tunnel.
- **Streamlit admin** — the host's UI; reads/writes the same DB. Only needs to run
  when the host is using it.

## 5. Data model (initial)

**guests**
- `id`, `name`, `phone` (E.164, e.g. `+9725...`), `language` (`he`/`en`)
- `status` (`pending` → `invited` → `confirmed` / `declined`)
- `conversation_state` (`none` / `awaiting_yesno` / `awaiting_details` / `done`)
- `reminder_count`, `last_reminded_at`, `invited_at`, `created_at`

**responses** (the RSVP result; one per guest)
- `guest_id`, `attending` (bool), `party_size` (int), `dietary` (text),
  `note` (text), `responded_at`

**messages** (audit log)
- `id`, `guest_id`, `direction` (`in`/`out`), `type`
  (`template`/`text`/`interactive`/`button`), `body`, `wa_message_id`,
  `timestamp`, `raw_json`

## 6. Message flows

### Outbound invite (the first contact)
Outside the 24-hour window, Meta only allows **pre-approved template messages**. The
invite is a template with **Yes / No quick-reply buttons**, in the guest's language.
Sending sets `status=invited`, `conversation_state=awaiting_yesno`.

### Incoming reply (webhook → DB)
1. Meta POSTs the event to `/webhook`.
2. **Button tap (Yes/No)** → deterministic: set `attending`. If Yes → ask follow-up
   ("How many of you? Any dietary needs? Anything to add?") and set
   `awaiting_details`. If No → `status=declined`, `done`.
3. **Free-text reply** (within the now-open 24h window) → send to OpenAI for
   **structured extraction** → `{attending, party_size, dietary, note}` as JSON
   (tool/function calling), handling Hebrew + English. Store in `responses`.
4. Fire a **per-reply notification** to the host.

### Reminders (scheduler)
APScheduler job (e.g. hourly) finds guests with `status=invited`,
`invited_at` older than *N* days, and `reminder_count < max`, then re-sends.
**Note:** reminders land outside the 24h window, so they must also be a **template
message** (reuse the invite template or a dedicated reminder template).

## 7. WhatsApp Cloud API specifics to handle

- **Webhook verification:** Meta sends a `GET /webhook` with `hub.challenge` +
  a verify token you choose; echo the challenge back. Then it POSTs message events.
- **Sending:** `POST https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages`
  with a Bearer token.
- **Templates:** created in WhatsApp Manager, per language, need approval
  (usually minutes–hours). Quick-reply buttons are supported in templates.
- **Access token gotcha:** the token shown in the dashboard is **temporary (~24h)**.
  For ongoing dev, create a **System User** in Meta Business Settings and issue a
  **permanent token** with WhatsApp permissions.
- **Tunnel URL gotcha:** a quick tunnel URL changes on each restart — you'd have to
  re-save the webhook URL in the Meta dashboard. Use a **named cloudflared tunnel**
  or an **ngrok reserved domain** to keep it stable.

## 8. Phased roadmap

**Phase 0 — Setup**
- Meta developer account → create app → add WhatsApp product → get test number +
  `PHONE_NUMBER_ID` + token; register your test recipient number(s).
- OpenAI API key. Python deps. `.env`. SQLite schema.

**Phase 1 — v1 (outbound RSVP, the core build)**
- Admin UI: guest CRUD, language per guest, "Send invites", "Remind pending".
- Engine: send invite template; `/webhook` verify + receive; hybrid parsing
  (buttons + OpenAI); persist responses; per-reply host notification.
- Dashboard: totals (coming / declined / pending), headcount sum, dietary
  breakdown; CSV export.
- Auto-reminder scheduler.

**Phase 2 — Two-way**
- Handle guest-initiated messages and changes ("actually we'll be 4", questions).
- Optional small LLM Q&A about event details.

**Phase 3 — Optional**
- Deploy to a free always-on cloud tier (so reminders fire without your laptop on).
- Richer analytics, multiple events / reusability.

## 9. Proposed project structure

```
whatsapp-guest-confirm/
├── README.md
├── PLAN.md
├── .gitignore
├── .env.example
├── requirements.txt
├── app/                     # FastAPI engine
│   ├── config.py            # settings from env (pydantic-settings)
│   ├── db.py                # SQLAlchemy engine/session (WAL)
│   ├── models.py            # Guest, Response, Message
│   ├── whatsapp.py          # Cloud API client (templates/text/interactive)
│   ├── parser.py            # OpenAI structured extraction
│   ├── conversation.py      # flow state machine
│   ├── reminders.py         # APScheduler job
│   ├── notify.py            # host notification
│   └── webhook.py           # FastAPI app (GET verify + POST events)
├── admin/
│   └── dashboard.py         # Streamlit admin + dashboard
├── data/                    # sqlite db (gitignored)
└── tests/
```

Run with: `uvicorn app.webhook:app --reload` and `streamlit run admin/dashboard.py`,
plus the tunnel pointing at port 8000.

## 10. What you'll need to provide (Phase 0 checklist)

- [ ] Facebook/Meta account → developers.facebook.com → **Create App** (Business type)
- [ ] Add **WhatsApp** product → note the **test phone number** + `PHONE_NUMBER_ID`
- [ ] Add your phone as a **test recipient** (verify with the code Meta sends)
- [ ] Generate an access token (temporary now; permanent System User token later)
- [ ] An **OpenAI API key**
- [ ] Install a tunnel: `cloudflared` (or `ngrok`)

## 11. Risks / open items

- **Template approval** — bilingual invite/reminder templates must be approved before
  outbound works; trivial content usually approves fast.
- **LLM accuracy on Hebrew free text** — mitigated by buttons for the critical yes/no;
  LLM only fills count/dietary/note, and we validate its JSON.
- **SQLite concurrency** — two processes; WAL mode + short transactions handle this fine
  at personal scale.
- **Token & tunnel stability** — see §7 gotchas; set up permanent token + named tunnel
  early to avoid repeated reconfiguration.
```
