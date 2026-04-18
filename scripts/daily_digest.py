#!/usr/bin/env python3
"""
Apollo daily digest generator.

Prepends yesterday's campaign + mailbox stats to a markdown dashboard file.

Run manually or via cron (e.g. 8am local = 13:00 UTC):
    0 13 * * 1-5 APOLLO_API_KEY=... VAULT_ROOT=/path/to/vault python3 daily_digest.py

Env vars:
  APOLLO_API_KEY   Required. Master key.
  VAULT_ROOT       Path to vault root. Digest is written to
                   $VAULT_ROOT/Dashboards/Apollo Daily.md
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import urllib.request
from pathlib import Path

try:
    import yaml  # type: ignore
except ImportError:
    yaml = None

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.yaml"
VAULT_ROOT = Path(os.environ.get("VAULT_ROOT", str(Path.home() / "vault")))
DASH_PATH = VAULT_ROOT / "Dashboards" / "Apollo Daily.md"
API_BASE = "https://api.apollo.io/api/v1"
API_KEY = os.environ.get("APOLLO_API_KEY", "").strip()


def _get(path: str) -> dict:
    # Cloudflare on apollo.io blocks the default python-urllib user agent with
    # error 1010 — spoof a normal UA.
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        headers={
            "X-Api-Key": API_KEY,
            "Content-Type": "application/json",
            "User-Agent": "apollo-mcp-digest/1.0 (curl/8.0.0)",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    text = CONFIG_PATH.read_text()
    if yaml is not None:
        return yaml.safe_load(text) or {}
    out: dict = {"sequence_labels": {}, "mailbox_labels": {}}
    current = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("sequence_labels:"):
            current = "sequence_labels"
        elif s.startswith("mailbox_labels:"):
            current = "mailbox_labels"
        elif current and ":" in s and not s.startswith("#"):
            k, _, v = s.partition(":")
            out[current][k.strip().strip('"')] = v.strip().strip('"')
    return out


def build_digest() -> str:
    if not API_KEY:
        return "- Missing APOLLO_API_KEY"
    cfg = _load_config()
    seq_labels = cfg.get("sequence_labels", {})
    mbx_labels = cfg.get("mailbox_labels", {})

    try:
        seq_resp = _get("/emailer_campaigns/search?per_page=100")
        mbx_resp = _get("/email_accounts")
    except Exception as exc:
        return f"- Apollo API failed: {exc}"

    sequences = [c for c in seq_resp.get("emailer_campaigns", []) if not c.get("archived")]
    mailboxes = mbx_resp.get("email_accounts", [])

    lines = [f"### {dt.date.today().isoformat()}", ""]

    # Mailbox health
    lines.append("**Mailboxes**")
    for m in mailboxes:
        email = m.get("email", "?")
        label = mbx_labels.get(email, "")
        dscore = (m.get("deliverability_score") or {})
        score = dscore.get("deliverability_score", "—")
        open_r = dscore.get("avg_open_rate", 0) or 0
        reply_r = dscore.get("avg_reply_rate", 0) or 0
        sent = dscore.get("avg_daily_sent", 0) or 0
        lines.append(
            f"- `{email}` ({label}) — score {score} · 7d avg {sent:.0f}/day · "
            f"open {open_r*100:.1f}% · reply {reply_r*100:.1f}%"
        )
    lines.append("")

    # Active sequences
    lines.append("**Sequences (active, non-archived)**")
    if not sequences:
        lines.append("- (none)")
    for s in sequences:
        sid = s.get("id", "")
        label = seq_labels.get(sid, s.get("name") or "(unnamed)")
        active = s.get("active")
        contacts = s.get("num_contacts", 0)
        lines.append(f"- `{sid[:8]}` {label} · active={active} · contacts={contacts}")
    lines.append("")
    return "\n".join(lines)


def prepend_to_dashboard(digest: str) -> None:
    if not DASH_PATH.exists():
        print(f"WARN: dashboard not found at {DASH_PATH}")
        return
    text = DASH_PATH.read_text()
    anchor = "## Recent digests\n\n_Auto-prepended by daily_digest.py. Newest first._\n"
    if anchor not in text:
        print("WARN: anchor not found, appending to end")
        DASH_PATH.write_text(text + "\n\n" + digest + "\n")
        return
    before, _, after = text.partition(anchor)
    DASH_PATH.write_text(before + anchor + "\n" + digest + "\n\n" + after.lstrip("\n"))
    print(f"Digest prepended to {DASH_PATH}")


def main() -> int:
    digest = build_digest()
    print(digest)
    prepend_to_dashboard(digest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
