# WhatsApp RSVP Bot — Project Plan

> **RSVP** = *Répondez s'il vous plaît* ("please reply") — the request for invited
> guests to confirm whether they'll attend. This bot collects those confirmations
> over WhatsApp.

## 1. Summary

A **for-fun personal project** (no real event — built for learning/portfolio, $0 to
build and test). A WhatsApp bot that:

1. Lets the Host manage the invitation list in a small Streamlit app.
2. Sends each Invitation a WhatsApp message asking them to confirm attendance.
3. Understands their reply — *attending? / how many / dietary / free-text note* —
   using tappable buttons **plus** an LLM to parse messy free text.
4. Automatically reminds Invitations that haven't responded.
5. Shows the Host a live dashboard, notifies them on each reply, and exports CSV.

Messages are **bilingual** — Hebrew or English chosen per Invitation.

## 2. Decisions (locked)

| Area | Decision |
|------|----------|
| Use case | Personal, one-time (for-fun) event |
| Flow — v1 | **Bot-initiated outbound** ("Will you attend?") + capture replies, **including RSVP changes** (latest inbound Reply always wins — no time limit) |
| Flow — later (Phase 2) | Guest-initiated **Q&A** (answer questions about the event) |
| WhatsApp integration | **Official WhatsApp Business Cloud API** (Meta) — free at test scale |
| Language | Python 3.12 |
| Backend / engine | **FastAPI** (webhook + sending + scheduler) |
| Host app / dashboard | **Streamlit** (separate process, shares the DB) |
| Storage | **SQLite** (WAL mode), via SQLAlchemy |
| Invitation list source | The Streamlit app (add/edit invitations) |
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
                 │      Streamlit app      │   add/edit invitations, send invites,
                 │   (host/dashboard.py)   │   live dashboard, CSV export
                 └───────────┬─────────────┘
                             │ read/write
                       ┌─────▼──────┐
                       │  SQLite DB │  invitations · rsvps · message log
                       │ (WAL mode) │
                       └─────▲──────┘
                             │ read/write
   contacts' phones  ┌───────┴─────────────┐      ┌──────────────┐
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
- **Streamlit app** — the Host's UI; reads/writes the same DB. Only needs to run
  when the Host is using it.

## 5. Data model (initial)

**event** (single row — set by the Host before the bot runs, via "Event setup" in the Streamlit app)
- Couple names as first/last per partner, per language (`partner1_first_en`, `partner1_last_en`,
  `partner2_first_en`, `partner2_last_en` + the four `_he` twins); `couple_name_en`/`couple_name_he`
  are composed display properties ("Ada Cohen & Bo Levi" / "עדה כהן ובו לוי")
- `event_date`, `image_path` (optional)
- Exactly **one** Event per deployment; the implicit parent of every Invitation. Drives the
  invite template (couple names + optional header image) and acts as the **reminder cutoff**
  (no reminders fire after `event_date`). Also the fact source for Phase 2 Q&A.

**invitations**
- `id`, `name`, `phone` (E.164, e.g. `+9725...`, **`UNIQUE`** — the natural key; Host input is
  validated + canonicalized to E.164 at entry via `phonenumbers`, region `IL` as fallback,
  `+countrycode` overrides for non-IL; invalid input is rejected, never stored),
  `language` (`he`/`en`)
- `status` (`draft` → `invited` → `confirmed` / `declined`) — `draft` = added but invite not
  yet sent
- `conversation_state` (`none` / `awaiting_yesno` / `awaiting_details` / `done`)
- `reminder_count`, `last_reminded_at`, `invited_at`, `created_at`
- **`status` vs `conversation_state` are orthogonal**, written together in one transaction:
  `status` = the RSVP *outcome* (drives the dashboard + reminder eligibility);
  `conversation_state` = the *chat router* (what the next inbound means). `done` is **not** a
  lock — a Reply after `done` is still processed (see "RSVP changes" below).

**State transitions** (`status` / `conversation_state` / RSVP are written together in one txn):

| Trigger | → status | → conversation_state | RSVP effect |
|---|---|---|---|
| Host adds Invitation | `draft` | `none` | — |
| Host sends invite | `invited` | `awaiting_yesno` | — |
| Reminder re-sent | `invited` (unchanged) | `awaiting_yesno` | — |
| Button **Yes** | `confirmed` | `awaiting_details` | `attending=true`, `party_size=NULL` |
| Button **No** | `declined` | `done` | `attending=false`, `party_size=NULL` |
| Details after Yes | `confirmed` | `done` | size / dietary / note filled |
| First-contact free-text Yes | `confirmed` | `done` if size given, else `awaiting_details` | attending=true (+size if given) |
| Free-text / flip → No | `declined` | `done` | `attending=false`, `party_size=NULL` (cleared) |
| Flip → Yes (was No) | `confirmed` | `awaiting_details` | `attending=true`, `party_size=NULL` |
| Question / other | unchanged | unchanged | nothing (log + notify Host) |
| Host **Re-invite** (manual) | `invited` | `awaiting_yesno` | RSVP reset |

**rsvps** (the RSVP result; one per Invitation)
- `invitation_id`, `attending` (bool), `party_size` (int, **nullable**), `dietary` (text),
  `note` (text), `responded_at`
- `party_size = NULL` means *attending but size not yet reported* (distinct from `0`). An
  RSVP that is `attending=true` with `party_size=NULL` is **confirmed but incomplete** — it
  is never silently coerced to 0 or 1.
- **Invariant: `attending=false ⇒ party_size IS NULL`.** A decline (button No, free-text "we
  can't come", or a Yes→No flip) clears `party_size` — a "would've been 4" number is never
  stored on the RSVP (it stays in the `messages` audit log). This keeps `SUM(party_size)`
  correct even if a query forgets the `attending=true` filter. Enforce as an app-level guard /
  `CHECK`.

**messages** (audit log)
- `id`, `invitation_id`, `direction` (`in`/`out`), `type`
  (`template`/`text`/`interactive`/`button`), `body`, `wa_message_id` (**`UNIQUE`** — the
  webhook idempotency key; a re-delivered event hits this constraint and is skipped),
  `timestamp`, `raw_json`

## 6. Message flows

### Outbound invite (the first contact)
Outside the 24-hour window, Meta only allows **pre-approved template messages**. The
invite is a template with **Yes / No quick-reply buttons**, in the Invitation's language. It renders
the **Event** details — the couple's names in that language, and the optional image as the
template header. Sending sets `status=invited`, `conversation_state=awaiting_yesno`.

### Incoming reply (webhook → DB)
**Four webhook-robustness rules run before the per-message logic** (the endpoint is public,
Meta's delivery is at-least-once, and it multiplexes event types onto it):
- **Verify the signature first:** reject any POST whose `X-Hub-Signature-256` doesn't match the
  HMAC-SHA256 of the raw body (App Secret key) — *before* parsing or touching the DB.
- **Ack fast, then process:** return `200` immediately, *then* run the (possibly slow) OpenAI
  parse — so Meta doesn't time out and retry.
- **Idempotency:** insert the inbound row first, keyed on `wa_message_id` (`UNIQUE`); a
  duplicate delivery hits the constraint → ack `200` and stop (no re-notify, no re-write).
- **Event type:** process `value.messages[]` (real inbound → the flow below);
  `value.statuses[]` are `sent`/`delivered`/`read` receipts for *our* outbound — log or
  ignore in v1, never treat as a Reply.

1. Meta POSTs the event to `/webhook`. **Match the sender:** prepend `+` to the webhook
   `wa_id`, normalize to E.164, and look up the Invitation by `phone`. **No match → it's a
   Reply with no Invitation:** log it to `messages`, notify the Host once ("📩 message from an
   unknown number…"), then stop — never auto-create an Invitation.
2. **Button tap (Yes/No)** → deterministic: set `attending`. If Yes → `status=confirmed`,
   `party_size=NULL` (confirmed but incomplete), ask follow-up ("How many of you? Any
   dietary needs? Anything to add?") and set `awaiting_details`. If the Invitation never replies,
   the RSVP stands as *attending, size unknown* — it is never coerced to 0/1. If No →
   `status=declined`, `party_size=NULL`, `done`.
3. **Free-text reply** → send to OpenAI for **structured
   extraction** (tool/function calling, Hebrew + English) →
   `{intent, attending, party_size, dietary, note, confidence}`, where `intent` is a **closed
   enum**: `rsvp_yes | rsvp_no | provide_details | change | question | other`. **Control flow
   keys off `intent`, not `confidence`** — self-reported confidence is poorly calibrated, so it
   is **logged only** (optionally routing very-low-confidence cases to the Host), never the sole
   gate. Rules:
   - **`null` = "couldn't determine" → never overwrite a known value with `null`.** Only
     non-null fields update the RSVP.
   - **Flip `attending` only on an explicit `rsvp_yes`/`rsvp_no`** that contradicts the current
     value. A tapped Yes/No **button is authoritative** for an answer already given; the LLM
     overrides it only on an unambiguous yes/no statement. **If the first contact is free text**
     (state `awaiting_yesno`, no button yet — e.g. "yes, 3 of us, vegetarian"), there is no
     button to defer to: the LLM sets `attending` from scratch and may complete the RSVP in one
     shot.
   - **`intent = question`/`other` (or anything ambiguous) → touch nothing**, log it, notify
     the Host to decide ("Dana asked: …"); no auto-answer in v1 (Phase 2 Q&A).
   - **Validate** before writing: `party_size` a positive int in a sane range (e.g. 1–20);
     on JSON or validation failure, don't overwrite — notify the Host.
   Store the result in `rsvps`.
4. Fire a **per-reply notification** to the Host.

### RSVP changes (latest inbound Reply always wins — no time limit)
An Invitation's RSVP always reflects its **most recent Reply**, with no time limit: a new
free-text reply overwrites `party_size`/`dietary`/`note`, and a Yes↔No change flips
`attending` + `status` (re-opening or closing the follow-up accordingly; a flip to No also
**clears `party_size`** per the invariant in §5). The `rsvps` row
stays single (one per Invitation); the full history lives in the `messages` audit log.
**Every change fires a Host notification.**

**The 24h window is an *outbound-send* constraint only — never a gate on accepting changes.**
WhatsApp always delivers inbound messages, and each inbound *reopens* the 24h window, so a
change is always processed the moment it arrives. The window only governs *how the bot may
reply*: within 24h of the Invitation's last inbound it can send free-form text (e.g. "Got it — 4
it is"); with no recent inbound (e.g. a reminder to a silent Invitation) the outbound must be
a pre-approved **template**.

### Host notifications (in-dashboard activity feed — v1)
"Notify the Host" means **append to a live activity feed shown in the Streamlit app** —
*not* a WhatsApp message to the Host. WhatsApp-to-Host is ruled out for v1: the Host is just
another WhatsApp user, so per-reply push would hit the same 24h-window/template wall (and a
template can't carry ad-hoc text like "Dana asked: is there parking?"). The feed is driven off
the message log and surfaces each reply, RSVP change, unknown-number message, question
(`intent=question`), and validation failure — newest first. `notify.py` stays a thin seam so a free push channel
(ntfy/Telegram) can be dropped in later (optional Phase 1.5) without touching call sites.

### Reminders (scheduler)
APScheduler job (e.g. hourly) finds invitations with `status=invited`,
`invited_at` older than *N* days, and `reminder_count < max`, then re-sends —
**but never after `event.event_date`** (the event has passed; the loop stops).
**Note:** reminders land outside the 24h window, so they must also be a **template
message** (reuse the invite template or a dedicated reminder template).

**Scope (deliberate):** the auto-reminder chases only *silent* Invitations (`status=invited`).
An Invitation that tapped **Yes** but never sent its count (`status=confirmed`,
`party_size=NULL`) is **not** auto-chased — an accepted v1 gap, since the dashboard already
surfaces unknown-size separately so the headcount never silently undercounts. The Host chases
these individually via a **manual "nudge for details" button** in the Streamlit app (uses a
count-prompt template when outside the 24h window).

## 7. WhatsApp Cloud API specifics to handle

- **Webhook verification:** Meta sends a `GET /webhook` with `hub.challenge` +
  a verify token you choose; echo the challenge back. Then it POSTs message events.
- **Delivery semantics:** webhook delivery is **at-least-once** (Meta retries if you're slow
  to `200`), and the same endpoint also receives **status callbacks** (`sent`/`delivered`/
  `read`). Dedup on `wa_message_id` and branch on `messages` vs `statuses` (see §6).
- **Webhook security (signature):** the `GET` verify token only guards the subscription
  handshake — ongoing `POST`s are protected by **`X-Hub-Signature-256`**, an HMAC-SHA256 of the
  **raw body** keyed with your **App Secret**. Recompute it, **constant-time compare**, drop on
  mismatch — otherwise anyone hitting the public tunnel URL could forge replies or trigger
  OpenAI calls. Read the raw bytes *before* JSON parsing.
- **Phone matching:** the webhook delivers the sender as `wa_id` — **bare digits, no `+`**
  (e.g. `972541234567`). Normalize both stored numbers and inbound `wa_id` to E.164
  (`phonenumbers`) so they compare equal; `invitations.phone` is the `UNIQUE` natural key.
  - **Host input is validated + canonicalized at entry** (Streamlit app): parse with region
    `IL` as a *fallback*, require `is_valid_number`, store **only** canonical E.164, reject
    invalid input inline — never persist an unparseable number.
  - **A `+countrycode` prefix always overrides the IL fallback** → enter non-Israeli numbers as
    `+1…`, `+44…`, etc. (the UI hints this).
  - **Inbound:** prepend `+` to `wa_id` and parse (already international — region irrelevant);
    no match or parse failure → the unknown-number path (§6: log + notify, never auto-create).
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
- Streamlit app: **Event setup** (couple names en/he, date, optional image), Invitation CRUD
  (phone validated + canonicalized at entry), language
  per Invitation, "Send invites", "Remind non-responders" (targets `invited` + no reply), and
  per-Invitation "Nudge for details" (confirmed-but-incomplete) and "Re-invite" (manual; resets
  a `declined`/`draft` Invitation to `invited` + `awaiting_yesno` and re-sends the template).
- Engine: send invite template; `/webhook` verify + receive (validate `X-Hub-Signature-256`;
  idempotent: dedup on `wa_message_id`, ignore status callbacks); sender matching; hybrid parsing
  (buttons + OpenAI); persist RSVPs (latest inbound Reply always wins); per-reply Host
  notification.
- Dashboard: totals (**Coming** `confirmed` / **Declined** `declined` / **Awaiting reply**
  `invited` / **Not invited** `draft`); headcount = **known heads + a count of
  attending invitations of unknown size** (never silently undercounts); dietary breakdown;
  CSV export (unknown size exported as 1, flagged).
- Auto-reminder scheduler.

**Phase 2 — Guest-initiated Q&A**
- RSVP *changes* ("actually we'll be 4") are already handled in v1; Phase 2 adds answering
  guest-initiated **questions** about the event (small LLM Q&A over event details).

**Phase 3 — Optional**
- Deploy to a free always-on cloud tier (so reminders fire without your laptop on).
- Richer analytics, multiple events / reusability.

## 9. Proposed project structure

```
whatsapp-rsvp-bot/
├── README.md
├── PLAN.md
├── .gitignore
├── .env.example
├── requirements.txt
├── app/                     # FastAPI engine
│   ├── config.py            # settings from env (pydantic-settings)
│   ├── db.py                # SQLAlchemy engine/session (WAL)
│   ├── models.py            # Invitation, Rsvp, Message
│   ├── whatsapp.py          # Graph API client — templates/text/interactive (injectable seam)
│   ├── parser.py            # OpenAI structured extraction (injectable seam)
│   ├── conversation.py      # flow state machine
│   ├── reminders.py         # APScheduler job
│   ├── notify.py            # host notification → dashboard activity feed (swappable seam)
│   └── webhook.py           # FastAPI app (GET verify + POST events)
├── host/
│   └── dashboard.py         # Streamlit app (Host UI) + dashboard
├── data/                    # sqlite db (gitignored)
└── tests/                   # pytest: state machine, parser handling, matching, fixtures
```

Run with: `uvicorn app.webhook:app --reload` and `streamlit run host/dashboard.py`,
plus the tunnel pointing at port 8000.

## 10. What you'll need to provide (Phase 0 checklist)

- [ ] Facebook/Meta account → developers.facebook.com → **Create App** (Business type)
- [ ] Add **WhatsApp** product → note the **test phone number** + `PHONE_NUMBER_ID`
- [ ] Add your phone as a **test recipient** (verify with the code Meta sends)
- [ ] Generate an access token (temporary now; permanent System User token later)
- [ ] Note your **App Secret** (Meta App → Settings → Basic) — needed for webhook signature validation
- [ ] An **OpenAI API key**
- [ ] Install a tunnel: `cloudflared` (or `ngrok`)
- [ ] Define the **Event** in the Streamlit app: couple names (English + Hebrew), date, optional image

## 11. Risks / open items

- **Template approval** — bilingual invite/reminder templates must be approved before
  outbound works; trivial content usually approves fast.
- **LLM accuracy on Hebrew free text** — control flow keys off a discrete `intent` enum, not a
  self-reported `confidence` float (poorly calibrated). The LLM flips `attending` only on an
  explicit `rsvp_yes`/`rsvp_no`; ambiguous cases change nothing and route to the Host. It fills
  count/dietary/note, returns `null` for anything it can't determine (and `null` never
  overwrites a known value), and we validate its JSON before writing.
- **SQLite concurrency** — two processes; WAL mode + short transactions handle this fine
  at personal scale.
- **Token & tunnel stability** — see §7 gotchas; set up permanent token + named tunnel
  early to avoid repeated reconfiguration.
- **Public webhook** — the tunnel URL is internet-reachable; `X-Hub-Signature-256` validation
  (App Secret HMAC over the raw body) is the gate that keeps forged RSVPs and OpenAI-cost abuse
  out. The `GET` verify token only covers the setup handshake.
- **Silent headcount undercount** — an Invitation can be attending with party size unknown
  (tapped Yes, never sent a number). Mitigated: `party_size` is nullable and the dashboard
  reports unknown-size invitations separately rather than counting them as 0. The bot does
  **not** auto-chase these (accepted gap); the Host nudges them manually from the Streamlit app.

## 12. Testing strategy

The hard dependencies — **Graph API** (sending), **OpenAI** (parsing), **Meta webhooks**
(incoming) — are external and non-deterministic, so the design makes them **swappable seams**
and tests the logic that's actually ours.

- **Tooling:** `pytest` + FastAPI `TestClient`; a throwaway **temp/in-memory SQLite** per test
  (real DB via SQLAlchemy — never mocked).
- **Injectable externals (build constraint):** `whatsapp.py` and `parser.py` are passed in as
  dependencies, not called at import time. Tests inject a **fake WhatsApp client** (records
  "sent template X to +972…") and a **parser stub** (returns a canned
  `{intent, attending, party_size, …}`) — no network, deterministic.
- **Webhook fixtures:** real Meta payload samples — button Yes, button No, Hebrew free text,
  English free text, a `statuses` callback, an unknown-number message, a duplicate
  `wa_message_id` — POSTed through `TestClient` to exercise the whole ingestion path.
- **Priority suites:** conversation state machine (table-driven) → parser-result handling
  (`null` never overwrites, range validation, flip rules, intent routing) → phone matching →
  signature + idempotency → the `declined ⇒ party_size IS NULL` invariant. Coverage % is not a
  goal (for-fun project).
- **Opt-in LLM eval:** a separate, manually-run suite hitting **real** OpenAI with ~10
  Hebrew/English phrases to sanity-check the prompt. Excluded from the default/CI run so the
  normal suite stays free, fast, and deterministic.
