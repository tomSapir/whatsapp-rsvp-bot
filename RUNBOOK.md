# Runbook — how to launch everything

Day-to-day operation of the bot. One-time setup (Meta app, templates, tokens) is at the
bottom — you only revisit that section when something breaks.

## TL;DR — start the whole stack

From the repo root:

```powershell
.\run.ps1
```

This opens three windows: the FastAPI engine, the ngrok tunnel, and the Streamlit Host
app. Close the windows (Ctrl+C) to stop.

Or start them manually, each in its own PowerShell terminal at the repo root:

| # | What | Command |
|---|------|---------|
| 1 | Engine (webhook + sender + reminders) | `.venv\Scripts\activate` then `uvicorn app.main:create_app --factory --port 8000` |
| 2 | Tunnel (public webhook URL) | `ngrok http --domain=chemicals-scalded-mundane.ngrok-free.dev 8000` |
| 3 | Host app (Streamlit UI) | `.venv\Scripts\activate` then `streamlit run host/dashboard.py` |

Ready when: terminal 1 says `Uvicorn running on …:8000`, terminal 2 shows a
`Forwarding https://… -> http://localhost:8000` line, and the Streamlit UI is open in
the browser (`http://localhost:8501`).

> The ngrok domain is reserved, so the webhook URL registered with Meta stays valid
> across restarts — **no Meta reconfiguration needed on a normal start**.

## Quick health checks

```powershell
# Engine answers locally (expect HTTP 403 — means it's up; the token in the URL is fake):
Invoke-WebRequest "http://localhost:8000/webhook?hub.mode=subscribe&hub.verify_token=x&hub.challenge=ping" -SkipHttpErrorCheck | Select-Object StatusCode

# Whole path internet → tunnel → engine (expect 200 and body `ping`; uses the real token from .env):
$t = ((Get-Content .env | Where-Object { $_ -match '^WEBHOOK_VERIFY_TOKEN=' }) -split '=', 2)[1].Trim()
Invoke-WebRequest "https://chemicals-scalded-mundane.ngrok-free.dev/webhook?hub.mode=subscribe&hub.verify_token=$t&hub.challenge=ping" -Headers @{'ngrok-skip-browser-warning'='1'} | Select-Object StatusCode, Content

# WhatsApp access token still valid (expect type SYSTEM_USER, expires Never):
$tok = ((Get-Content .env | Where-Object { $_ -match '^WHATSAPP_ACCESS_TOKEN=' }) -split '=', 2)[1].Trim()
(Invoke-RestMethod "https://graph.facebook.com/debug_token?input_token=$tok&access_token=$tok").data | Select-Object type, is_valid, expires_at
```

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Meta: "callback URL couldn't be validated" | Engine or tunnel not running — start terminals 1 + 2 first, then retry **Verify and save**. |
| Graph API `401` on send | Access token invalid/expired. Run the token health check above; regenerate via the System User if needed (one-time setup below). |
| Graph API `132001` on send | Template name or language code doesn't match an **approved** template (`wedding_invite`, `rsvp_reminder`, `rsvp_details_nudge`; languages `he`, `en`) — **in the Test WhatsApp Business Account** (ID `1757491848554374`). Templates created in the personal "Tom Sapir" WABA are invisible to the test number; clone them over with `scripts/clone_templates.py`. |
| Invite sends but no reply arrives in the dashboard | `messages` webhook field not subscribed (one-time setup below), or tunnel down. |
| `ModuleNotFoundError: No module named 'app'` from Streamlit | Run from the **repo root** (`streamlit run host/dashboard.py`), not from inside `host/`. |

## One-time setup (already done — for reference / disaster recovery)

1. **Meta app**: developers.facebook.com → My Apps → the app → **WhatsApp** product.
   `PHONE_NUMBER_ID`, App Secret (App settings → Basic), test recipients — all in `.env`.
2. **Permanent token**: business.facebook.com/settings → Users → **System users** →
   the `rsvp-bot` system user → Generate token (expiration **Never**, permissions
   `whatsapp_business_messaging` + `whatsapp_business_management`) → paste into `.env`
   as `WHATSAPP_ACCESS_TOKEN`.
3. **Webhook registration**: app dashboard → WhatsApp → Configuration → Webhook →
   **Edit**: callback URL `https://chemicals-scalded-mundane.ngrok-free.dev/webhook`,
   verify token = `WEBHOOK_VERIFY_TOKEN` from `.env` → **Verify and save** (engine +
   tunnel must be running). Then **Webhook fields → Manage → subscribe to `messages`**.
4. **Templates**: WhatsApp Manager → Message templates — `wedding_invite`,
   `rsvp_reminder`, `rsvp_details_nudge`, each in `he` + `en`, no `{{…}}` variables
   (the code sends them parameter-less). **They must live in the Test WhatsApp Business
   Account** (`1757491848554374`) — the account the test number belongs to — not the
   personal WABA; check the account picker in WhatsApp Manager before creating. All
   languages of one template name must share a category (UTILITY here).
