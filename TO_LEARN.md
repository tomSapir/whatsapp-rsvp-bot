# TO_LEARN

Things to look up and learn online later. Each item notes where it shows up in this project.

- [ ] **cloudflared / ngrok** — tunnel tools that give your laptop a stable public URL so
  Meta can reach `localhost`. Why it's needed: your machine has no public address, so the
  webhook can't find it without a tunnel. (PLAN.md §7 "Tunnel URL gotcha".)
- [ ] **`/webhook`** — what a webhook is: a public endpoint Meta *calls* (HTTP `POST`) to push
  incoming WhatsApp messages to your app, instead of your app polling for them. Includes the
  one-time `GET` verify handshake (`hub.challenge` + verify token) and the ongoing `POST`
  event delivery. (PLAN.md §6–§7.)
- [ ] **`X-Hub-Signature-256`** — the HMAC-SHA256 header Meta stamps on every webhook `POST`
  (keyed with your **App Secret**, computed over the raw body). Validating it proves the request
  really came from Meta, so nobody can forge RSVPs by POSTing to your public URL. Look up: HMAC,
  why constant-time comparison matters, and reading the raw body before JSON parsing. (PLAN.md §7.)
- [ ] **Graph API** — Meta's HTTP API you *call* to **send** WhatsApp messages
  (`POST https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages`, Bearer token). The
  outbound half; the webhook is the inbound half. Look up: message types (template / text /
  interactive), the Bearer access token, and API versioning (`v21.0`). (PLAN.md §7.)
- [ ] **FastAPI** — the Python web framework running the engine: serves the `/webhook` GET+POST
  endpoints, sends messages, and hosts the reminder scheduler. Look up: path operations, reading
  the raw `Request` body, `BackgroundTasks` (for ack-fast-then-process), and `TestClient` for
  tests. (PLAN.md §4, §9.)
