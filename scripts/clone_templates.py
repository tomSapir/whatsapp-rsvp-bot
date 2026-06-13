"""One-off: clone message templates between WhatsApp Business Accounts.

The six RSVP templates were created (and approved) in the personal WABA, but the test
sender number belongs to the app's Test WABA — and templates are only usable from the
account they live in. Reads every non-sample template from SOURCE, strips the read-only
``id``, and submits it to TARGET (where it goes through Meta review again).

Run: .venv/Scripts/python scripts/clone_templates.py
"""

import httpx

SOURCE_WABA = "27627846276821636"  # "Tom Sapir" — templates approved here
TARGET_WABA = "1757491848554374"  # "Test WhatsApp Business Account" — test number lives here
SKIP = {"hello_world"}  # Meta sample, already present in the target

with open(".env", encoding="utf-8") as f:
    token = next(
        line.split("=", 1)[1].strip()
        for line in f
        if line.startswith("WHATSAPP_ACCESS_TOKEN=")
    )

headers = {"Authorization": f"Bearer {token}"}
base = "https://graph.facebook.com/v21.0"

templates = httpx.get(
    f"{base}/{SOURCE_WABA}/message_templates",
    params={"fields": "name,language,category,components", "limit": 50},
    headers=headers,
).json()["data"]

for t in templates:
    if t["name"] in SKIP:
        continue
    payload = {
        "name": t["name"],
        "language": t["language"],
        "category": t["category"],
        "components": t["components"],
        "allow_category_change": True,
    }
    r = httpx.post(f"{base}/{TARGET_WABA}/message_templates", json=payload, headers=headers)
    status = r.json().get("status", r.json().get("error", {}).get("message", "?"))
    print(f"{t['name']}/{t['language']}: HTTP {r.status_code} -> {status}")
