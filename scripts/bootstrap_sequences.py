#!/usr/bin/env python3
"""
Apollo sequence bootstrapper.

Creates sequences end-to-end via API: campaign shell, steps, templates,
schedule attachment. Idempotent — skips sequences that already exist (by name).

Useful for version-controlling your sequence templates in code instead of
editing them manually in the Apollo UI.

Usage:
    APOLLO_API_KEY=... python3 bootstrap_sequences.py

Requires a MASTER API key.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
import urllib.error
from typing import Any, Optional

API_BASE = "https://api.apollo.io/api/v1"
API_KEY = os.environ["APOLLO_API_KEY"]
UA = "apollo-mcp/1.0 (curl/8.0.0)"

# Find your schedule ID via GET /emailer_schedules or the Apollo UI URL.
DEFAULT_SCHEDULE_ID = "REPLACE_WITH_YOUR_SCHEDULE_ID"


def call(method: str, path: str, body: Optional[dict] = None) -> dict:
    url = f"{API_BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "X-Api-Key": API_KEY,
            "Content-Type": "application/json",
            "User-Agent": UA,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode()[:500]
        raise RuntimeError(f"HTTP {e.code} on {method} {path}: {body_text}")


def find_existing(name: str) -> Optional[str]:
    resp = call("POST", "/emailer_campaigns/search", {"per_page": 100, "page": 1})
    for c in resp.get("emailer_campaigns", []):
        if not c.get("archived") and c.get("name") == name:
            return c["id"]
    return None


def create_campaign(name: str) -> str:
    resp = call("POST", "/emailer_campaigns", {
        "name": name,
        "permissions": "team_can_use",
    })
    cid = resp["emailer_campaign"]["id"]
    call("PATCH", f"/emailer_campaigns/{cid}", {
        "emailer_schedule_id": DEFAULT_SCHEDULE_ID,
    })
    return cid


def create_step(campaign_id: str, position: int, wait_days: int) -> dict:
    resp = call("POST", "/emailer_steps", {
        "emailer_campaign_id": campaign_id,
        "position": position,
        "type": "auto_email",
        "wait_time": wait_days,
        "wait_mode": "day",
    })
    return resp  # contains emailer_step + emailer_touch (with template_id)


def update_template(template_id: str, subject: str, body_html: str) -> None:
    call("PATCH", f"/emailer_templates/{template_id}", {
        "subject": subject,
        "body_html": body_html,
    })


def approve_touch(touch_id: str) -> None:
    """Move a touch out of 'to_be_reviewed' so the step actually sends.

    Apollo exposes this as POST /emailer_touches/{id}/approve with no body.
    PATCH on the touch returns 422 'undefined method []' — this action
    endpoint is the only way to approve programmatically.
    """
    call("POST", f"/emailer_touches/{touch_id}/approve")


def build_sequence(name: str, steps: list[dict]) -> str:
    """
    steps = [{"wait_days": int, "subject": str, "body_html": str}, ...]
    Returns campaign_id (or "SKIPPED_<id>" if a live sequence with that name exists).
    """
    existing = find_existing(name)
    if existing:
        print(f"  SKIP (already exists, non-archived): {name} -> {existing}")
        return f"SKIPPED_{existing}"

    cid = create_campaign(name)
    print(f"  created campaign: {cid}")
    for i, s in enumerate(steps, 1):
        resp = create_step(cid, position=i, wait_days=s["wait_days"])
        step_id = resp["emailer_step"]["id"]
        touch = resp["emailer_touch"]
        template_id = touch["emailer_template_id"]
        touch_id = touch["id"]
        update_template(template_id, s["subject"], s["body_html"])
        approve_touch(touch_id)
        print(f"    step {i}: wait={s['wait_days']}d  step={step_id[:8]} tpl={template_id[:8]}")
        time.sleep(0.3)  # be nice to the API
    return cid


def p(text: str) -> str:
    """Minimal text-to-HTML for Apollo body_html."""
    paragraphs = [t.strip() for t in text.strip().split("\n\n") if t.strip()]
    return "".join(
        "<p>" + para.replace("\n", "<br>") + "</p>" for para in paragraphs
    )


# ---------------------------------------------------------------------------
# Define your sequences here. Replace with your own copy.
# ---------------------------------------------------------------------------

EXAMPLE_SEQUENCE = [
    {
        "wait_days": 0,
        "subject": "Your subject line here",
        "body_html": p("""\
Hi {{first_name}},

Your opening line here. Keep it short — one observation or hook.

Your value prop in one sentence.

Your name"""),
    },
    {
        "wait_days": 4,
        "subject": "Following up",
        "body_html": p("""\
Hi {{first_name}},

Your follow-up. Add one new piece of information, not a repeat of step 1.

Your name"""),
    },
    {
        "wait_days": 7,
        "subject": "Last note",
        "body_html": p("""\
Hi {{first_name}},

Closing the loop. Either a soft CTA or a graceful exit.

Your name"""),
    },
]

SEQUENCES = [
    ("Your Sequence Name v1", EXAMPLE_SEQUENCE),
]


def main() -> int:
    for name, steps in SEQUENCES:
        print(f"Building: {name}")
        seq_id = build_sequence(name, steps)
        print(f"  result: {seq_id}\n")
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
