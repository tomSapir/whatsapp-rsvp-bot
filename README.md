# WhatsApp RSVP Bot

A bilingual (Hebrew / English) WhatsApp bot that collects guest RSVPs for a single
event — built as a **$0 for-fun / portfolio project** on Meta's official WhatsApp
Business Cloud API.

> **RSVP** — *Répondez s'il vous plaît* ("please reply"): the request for invited
> guests to confirm whether they'll attend.

## What it does

- The **Host** defines one **Event** (the couple's names in Hebrew + English, a date,
  an optional image) and manages the invitation list in a small **Streamlit app**.
- The bot sends each **Invitation** a WhatsApp template asking *"Will you attend?"* with
  tappable **Yes / No** buttons, in that Invitation's language.
- It understands replies — *attending? how many? dietary needs? a note?* — using the
  buttons **plus** an LLM that extracts structured data from messy free text (Hebrew or
  English).
- It **auto-reminds** non-responders (until they answer or the event date passes).
- The Host gets a **live dashboard** (Coming / Declined / Awaiting reply / Not invited,
  headcount, dietary breakdown), a per-reply **activity feed**, and a **CSV export**.

RSVP changes are handled too: the **latest reply always wins**.

## Status

🔧 **Code complete (M0–M9) — 146 offline tests passing.** What remains is the live Meta
side: template approval in WhatsApp Manager and the manual smoke test (STEPS M9.3–9.4).

- [x] Requirements & plan finalized — [PLAN.md](./PLAN.md)
- [x] Domain language defined — [CONTEXT.md](./CONTEXT.md)
- [x] Architecture defined
- [x] Implementation M0–M9 — [STEPS.md](./STEPS.md) (see [learning/](./learning) for
  plain-words write-ups of each milestone)
- [ ] WhatsApp template approval + live smoke test

## Architecture at a glance

Two processes share one SQLite (WAL) file:

- **FastAPI engine** — receives the Meta webhook (incoming replies), sends messages via
  the Graph API, and runs the reminder scheduler. Exposed to the internet via a tunnel
  (cloudflared / ngrok).
- **Streamlit app** — the Host's UI: Event setup, invitation CRUD, send/remind actions,
  and the live dashboard.

Full architecture, data model, message flows, and WhatsApp Cloud API specifics live in
**[PLAN.md](./PLAN.md)**.

## Tech stack

| Area | Choice |
|------|--------|
| Language | Python 3.12 |
| Engine | FastAPI (webhook + sending + scheduler) |
| Host UI / dashboard | Streamlit |
| Storage | SQLite (WAL) via SQLAlchemy |
| WhatsApp | Official WhatsApp Business Cloud API (Graph API `v21.0`) |
| Reply parsing | OpenAI structured extraction (tool calling) |
| Scheduler | APScheduler |
| Phone handling | phonenumbers (E.164) |

## Project layout

```
whatsapp-rsvp-bot/
├── app/                     # FastAPI engine
│   ├── config.py            # settings from env (pydantic-settings)
│   ├── db.py                # SQLAlchemy engine/session (WAL)
│   ├── models.py            # Event, Invitation, Rsvp, Message
│   ├── whatsapp.py          # Graph API client (injectable seam)
│   ├── parser.py            # OpenAI structured extraction (injectable seam)
│   ├── conversation.py      # flow state machine
│   ├── reminders.py         # APScheduler job
│   ├── notify.py            # host notification → activity feed
│   └── webhook.py           # FastAPI app (GET verify + POST events)
├── host/
│   └── dashboard.py         # Streamlit app (Host UI) + dashboard
├── data/                    # sqlite db (gitignored)
└── tests/                   # pytest
```

## Getting started

1. Work through the **Phase 0 checklist** in [PLAN.md §10](./PLAN.md) (Meta app, WhatsApp
   product, tokens, App Secret, OpenAI key, tunnel).
2. Copy `.env.example` → `.env` and fill in the secrets.
3. Install deps: `pip install -r requirements.txt`.
4. Run the three processes — `.\run.ps1` starts them all, or see **[RUNBOOK.md](./RUNBOOK.md)**
   for the manual commands, health checks, and troubleshooting:
   ```bash
   uvicorn app.main:create_app --factory --port 8000   # FastAPI engine + reminder job
   streamlit run host/dashboard.py                     # Host app
   ngrok http --domain=<reserved-domain> 8000          # stable public webhook URL → :8000
   ```
5. Register the tunnel's `/webhook` URL (with your verify token) in the Meta dashboard.

## Documentation

- **[RUNBOOK.md](./RUNBOOK.md)** — how to launch everything, health checks, troubleshooting
- **[PLAN.md](./PLAN.md)** — full project plan (architecture, data model, flows, risks)
- **[CONTEXT.md](./CONTEXT.md)** — domain glossary (the project's shared language)
- **[STEPS.md](./STEPS.md)** — step-by-step implementation sequence
- **[TO_LEARN.md](./TO_LEARN.md)** — concepts to study (webhooks, Graph API, HMAC signature, …)

---

*A for-fun personal project — no real event, $0 to build and test at Meta's free test scale.*
