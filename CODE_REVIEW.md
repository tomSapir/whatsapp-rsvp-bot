# Code Review — WhatsApp RSVP Bot

*Full, deep review · 2026-06-17 · against `main` @ `cfe238d` (125 tests passing, 10 skipped).*
*To act on in a later session — see [TODO.md](./TODO.md).*

## Verdict

Genuinely high-quality code — well above typical portfolio-project standard. Clean
architecture (injectable seams everywhere), domain invariants enforced at the database
level rather than only in app code, correct webhook security, and an excellent offline
test suite (deterministic, no network). Findings below are mostly robustness edge-cases,
not structural problems.

Strongest single thing: the **dependency-injection discipline**. `WhatsAppClient`,
`ReplyParser`, and `Notifier` are abstract contracts with real + fake implementations,
wired only in `app/main.py`. That's what makes the whole engine testable offline, and it's
done consistently.

Two points investigated and confirmed **NOT** bugs (verified empirically):
- `Message(timestamp=None)` (a bad Meta timestamp) does **not** violate `NOT NULL` —
  SQLAlchemy falls back to the `server_default`. Fine.
- `busy_timeout` is already **5000 ms** (the `sqlite3` DBAPI default), so the two-process
  SQLite write contention is mitigated. No missing pragma.

---

## Findings by severity

### 🟠 Medium — robustness gaps in the inbound pipeline

**1. OpenAI transport/API errors silently drop guest replies.**
`handle_text_reply` catches only `ParseError` (`conversation.py:130-138`), but
`OpenAIReplyParser.parse` (`parser.py:163-186`) can raise `RateLimitError`,
`APITimeoutError`, `APIError`, or raw `httpx` network errors — none are `ParseError`. The
chain `process_payload → _ingest_message → handle_text_reply` runs inside a FastAPI
`BackgroundTask` *after* the `200` was returned to Meta. So on an OpenAI hiccup:
- the exception escapes the background task and Starlette just logs it;
- Meta won't redeliver (it already got `200`);
- the inbound `Message` row was already committed as the idempotency gate
  (`webhook.py:164-169`), so even a manual redelivery is deduped and skipped;
- the Host is **never notified**.

Net: a guest's "yes, 4 of us!" vanishes during any OpenAI blip. The `ParseError` path
already does the right thing (notify the Host to handle manually) — broaden it to cover
transport errors too, and set an explicit timeout on the OpenAI client
(`parser.py:155-159`).

> **✅ Resolved (2026-06-18, `m13-inbound-resilience`):** added `ParserUnavailable(ParseError)`;
> `OpenAIReplyParser` now wraps `create()` (`OpenAIError → ParserUnavailable`) and sets a 20 s
> client timeout. `handle_text_reply` branches `ParserUnavailable` (Host: "couldn't process —
> handle manually", with the guest's text) before `ParseError`. Test:
> `test_parser_unavailable_touches_nothing_and_notifies`.

**2. Any background-processing failure is unrecoverable and invisible.** #1 generalized.
Because the dedup key is persisted *before* the message's effect (`webhook.py:165` commits,
then routing at `:179-186`), the design favors "never double-process" over "never lose."
Reasonable trade — but every exception after that commit permanently swallows the reply
with no trace for the Host. Wrap per-message routing in a try/except that notifies the Host
("couldn't process the reply from X") on any failure.

> **✅ Resolved (2026-06-18, `m13-inbound-resilience`):** the routing block in `_ingest_message`
> is now wrapped in `try/except Exception` → log, `session.rollback()`, notify the Host. The
> dedup row is committed separately, so idempotency holds while nothing is silently lost. Test:
> `test_background_failure_is_caught_and_host_notified`.

**3. Templates are sent with no `components` — guest-name personalization is impossible
(the `דנה` bug in TODO.md).**
`_send_template_and_log` (`actions.py:71-86`) and the reminder job (`reminders.py:104-106`)
call `send_template(phone, template_name, language)` with `components=None`. The client
supports components (`whatsapp.py:108-118`) but no caller passes them. Consequence depends
on the approved template:
- body has a `{{1}}` parameter → Meta **rejects** the send (missing parameter);
- sample name baked into the body text → **every** guest sees "דנה".

Decide the template contract and thread the guest's `name` through as a body component.

> **Correction (2026-06-18):** investigated against the live templates — this was a
> misdiagnosis. The approved templates have **no `{{…}}` variables** (RUNBOOK.md:70), so
> `components=None` is *correct*. The actual `דנה` bug was a content typo: the Hebrew
> `rsvp_details_nudge` body hardcoded "תום ודנה" instead of "תום ועמית". Fixed at the
> template level, no code change. Guest-name personalization (the `{{1}}` + components idea
> above) remains a valid *optional* enhancement — deferred. See TODO.md.

### 🟡 Low

**4. Send-before-commit ordering in `_apply_reply`.** `_send_follow_up` /
`_send_confirmation` do the real WhatsApp send *before* `session.commit()`
(`conversation.py:235-243`). If the commit then fails, the guest already received "How many
of you?" but the RSVP state change is rolled back — and the inbound dedup key is already
persisted, so redelivery won't repair it. Prefer commit-then-send, or make the ack
idempotent.

**5. CSV formula injection.** `export_csv` (`reporting.py:108-130`) writes guest-originated
`dietary`/`note` straight into cells. A value like `=HYPERLINK(...)` becomes a live formula
when the Host opens the CSV in Excel/Sheets. Fix: prefix any cell starting with
`= + - @ \t \r` with a `'`.

**6. `except IntegrityError` assumes "duplicate".** `webhook.py:166-169` treats *every*
`IntegrityError` as a re-delivered webhook. Today only the `wa_message_id` UNIQUE can
realistically trip there, so it's correct in practice — but another constraint would be
silently misclassified as a duplicate and dropped. Narrowing (inspect the failed
constraint) makes it robust to future schema changes.

### ⚪ Nits / notes (no action needed at this scale)

- **Timezone mix:** everything stored is naive UTC, but `event_date` and the dashboard
  countdown use the local calendar date. The reminder cutoff `now.date() >= event_date`
  (`reminders.py:81`) and "days to go" can be off by the UTC↔Israel offset around midnight.
- **`host_notifications` grows unbounded** — read is capped at 50 (`notify.py:55-61`); the
  table is never pruned. Negligible for one event.
- **Streamlit host UI has no auth** — fine *only* because `run.ps1` tunnels port 8000 (the
  webhook) but not 8501 (the dashboard). Note before ever exposing Streamlit remotely.
- **`_render_guest_actions`** uses `st.columns(3)` but only `btns[0]` and `btns[2]`
  (`dashboard.py:312-327`); `btns[1]` is dead — cosmetic.
- The test run emits a Starlette/httpx `TestClient` deprecation warning — harmless, track
  for a future dep bump.

---

## What's done especially well

- **Webhook hardening** (`webhook.py`): HMAC-SHA256 over the *raw* body before JSON parsing,
  constant-time compare, fast-ack + background processing, idempotency via UNIQUE
  `wa_message_id`, status-vs-message branching. Textbook.
- **DB-level invariants** (`models.py`): `CHECK (id = 1)` single-event,
  `attending=false ⇒ party_size IS NULL`, UNIQUE phone/rsvp/wa_message_id — and
  `foreign_keys=ON` is actually enabled (`db.py:52`), without which those would be silent
  no-ops.
- **"Unknown party size" semantics** handled with care end-to-end: never coerced to 0/1 in
  metrics, surfaced as `unknown_size_count`, and the CSV substitutes `1` only with a visible
  `size_unknown` flag (`reporting.py`, `CONTEXT.md`).
- **XSS** handled — `html.escape` on every interpolation into `unsafe_allow_html`
  (`dashboard.py:154-157, 246`).
- **Table-driven state-machine tests** (`test_conversation.py:110-217`) mirror the PLAN §5
  transition table row-for-row.

---

## Suggested order of work

The three Medium items share one root cause: **a failure after the idempotency commit has
no recovery path and no Host visibility.** A small try/except around message routing that
notifies the Host on any exception closes #1 and #2 together and is the highest-value
change. Then #3 (template `components`), which is already a known live blocker in TODO.md.
The Low items are quick, independent follow-ups.
