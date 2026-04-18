"""
Apollo.io MCP Server

FastMCP server exposing the operational surface of the Apollo.io REST API:
  Campaign health (primary daily use case)
  Sequence management (list, get, enroll, pause, activate)
  Email message logs (sends, opens, replies, bounces)
  Mailbox warmup / deliverability
  People + organization search and enrichment
  CRM contacts and credit usage tracking

Env vars:
  APOLLO_API_KEY         Required. MUST be a MASTER key for sequence/analytics endpoints.
  APOLLO_MCP_CONFIG      Optional. Path to config.yaml. Defaults to ./config.yaml next to server.py.
  APOLLO_MCP_TIMEOUT     Optional. HTTP timeout in seconds. Default 30.
"""

import asyncio
import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Any, Optional

import httpx
import yaml
from fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_HERE = Path(__file__).parent
_CFG_PATH = Path(os.environ.get("APOLLO_MCP_CONFIG", str(_HERE / "config.yaml")))
try:
    with open(_CFG_PATH) as f:
        CONFIG: dict = yaml.safe_load(f) or {}
except FileNotFoundError:
    CONFIG = {}

API_BASE = CONFIG.get("api_base", "https://api.apollo.io/api/v1")
TIMEOUT_SEC = float(os.environ.get("APOLLO_MCP_TIMEOUT", CONFIG.get("timeout_sec", 30)))
DAILY_TARGET = int(CONFIG.get("daily_send_target", 50))
CREDIT_POOL = int(CONFIG.get("monthly_credit_pool", 4000))
CREDIT_ALERT_PCT = float(CONFIG.get("credit_alert_pct", 0.75))
SEQUENCE_LABELS: dict = CONFIG.get("sequence_labels", {})
MAILBOX_LABELS: dict = CONFIG.get("mailbox_labels", {})

API_KEY = os.environ.get("APOLLO_API_KEY", "").strip()

# ---------------------------------------------------------------------------
# HTTP client with rate-limit backoff + TTL cache
# ---------------------------------------------------------------------------

_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_TTL = 300  # 5 minutes


def _cache_get(key: str) -> Optional[Any]:
    hit = _CACHE.get(key)
    if hit and (time.time() - hit[0] < _CACHE_TTL):
        return hit[1]
    return None


def _cache_set(key: str, value: Any) -> None:
    _CACHE[key] = (time.time(), value)


async def _request(
    method: str,
    path: str,
    params: Optional[dict] = None,
    body: Optional[dict] = None,
    cache_key: Optional[str] = None,
) -> dict:
    """Apollo HTTP call with 429 backoff + structured errors. Never raises."""
    if not API_KEY:
        return {"error": "APOLLO_API_KEY not set in environment"}

    if cache_key:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

    url = f"{API_BASE.rstrip('/')}{path}"
    headers = {
        "X-Api-Key": API_KEY,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
        # apollo.io sits behind Cloudflare and returns error 1010 when it sees
        # python-httpx/urllib default UAs. Pose as curl to avoid the block.
        "User-Agent": "apollo-mcp/1.0 (curl/8.0.0)",
    }

    for attempt in range(5):
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SEC) as client:
                resp = await client.request(
                    method, url, headers=headers, params=params, json=body
                )
            if resp.status_code == 429:
                delay = min(2 ** attempt + random.random(), 30)
                await asyncio.sleep(delay)
                continue
            if resp.status_code == 401:
                return {"error": "Invalid Apollo API key (401)"}
            if resp.status_code == 403:
                return {
                    "error": "Forbidden (403). Most likely needs MASTER API key.",
                    "path": path,
                }
            if resp.status_code >= 400:
                try:
                    body_text = resp.json()
                except Exception:
                    body_text = resp.text[:500]
                return {"error": f"HTTP {resp.status_code}", "path": path, "body": body_text}
            data = resp.json()
            if cache_key:
                _cache_set(cache_key, data)
            return data
        except httpx.HTTPError as e:
            if attempt == 4:
                return {"error": f"Network error: {type(e).__name__}: {e}"}
            await asyncio.sleep(1 + attempt)
    return {"error": "Max retries exceeded"}


async def _paginate(
    method: str,
    path: str,
    params: Optional[dict] = None,
    body: Optional[dict] = None,
    key: str = "contacts",
    max_pages: int = 50,
    per_page: int = 100,
) -> list[dict]:
    """Iterate pages up to max_pages. Apollo returns `pagination.total_pages`."""
    out: list[dict] = []
    page = 1
    while page <= max_pages:
        p = dict(params or {})
        p["page"] = page
        p["per_page"] = per_page
        if body is not None:
            b = dict(body)
            b["page"] = page
            b["per_page"] = per_page
            data = await _request(method, path, body=b)
        else:
            data = await _request(method, path, params=p)
        if "error" in data:
            out.append(data)
            break
        chunk = data.get(key) or data.get("results") or []
        out.extend(chunk)
        pagination = data.get("pagination") or {}
        total_pages = int(pagination.get("total_pages") or 0)
        if total_pages and page >= total_pages:
            break
        if not chunk:
            break
        page += 1
    return out


# ---------------------------------------------------------------------------
# MCP app
# ---------------------------------------------------------------------------

mcp = FastMCP("apollo")


def _label_sequence(seq_id: str, fallback_name: str = "") -> str:
    return SEQUENCE_LABELS.get(seq_id) or fallback_name or seq_id


def _label_mailbox(email: str) -> str:
    return MAILBOX_LABELS.get(email, email)


# ─── Campaign health (the daily use case) ──────────────────────────────────

@mcp.tool
async def apollo_campaign_health(days_back: int = 1) -> dict:
    """Return campaign health digest across all sequences + mailboxes.

    Shows: today's sends per mailbox vs daily target, opens/replies/bounces
    per sequence, and flags A/B variant data. Designed to fit in <2k tokens.

    Args:
        days_back: How many days of activity to report (default 1 = last 24h).
    """
    # Pull sequences, email accounts, and recent messages in parallel
    seqs_task = apollo_sequences_list()
    accounts_task = apollo_mailbox_warmup()
    messages_task = apollo_messages_search(days_back=days_back, per_page=100, max_pages=10)
    seqs, accounts, messages = await asyncio.gather(seqs_task, accounts_task, messages_task)

    if isinstance(messages, dict) and "error" in messages:
        return {"error": "messages_search failed", "detail": messages}

    sends_by_mailbox: dict[str, int] = {}
    by_sequence: dict[str, dict] = {}
    totals = {"sent": 0, "opened": 0, "replied": 0, "bounced": 0, "clicked": 0}

    for m in (messages.get("emailer_messages") or messages.get("results") or []):
        from_email = m.get("email_account", {}).get("email") or m.get("from_email") or "unknown"
        sends_by_mailbox[from_email] = sends_by_mailbox.get(from_email, 0) + 1

        seq_id = m.get("emailer_campaign_id") or "(none)"
        seq = by_sequence.setdefault(seq_id, {
            "sequence_id": seq_id,
            "label": _label_sequence(seq_id),
            "sent": 0, "opened": 0, "replied": 0, "bounced": 0, "clicked": 0,
        })
        seq["sent"] += 1
        totals["sent"] += 1
        if m.get("opened_at") or m.get("opened"):
            seq["opened"] += 1
            totals["opened"] += 1
        if m.get("replied_at") or m.get("replied"):
            seq["replied"] += 1
            totals["replied"] += 1
        if m.get("bounced_at") or m.get("bounced"):
            seq["bounced"] += 1
            totals["bounced"] += 1
        if m.get("clicked_at") or m.get("clicked"):
            seq["clicked"] += 1
            totals["clicked"] += 1

    def _pct(n: int, d: int) -> str:
        return f"{(100*n/d):.1f}%" if d else "n/a"

    digest = {
        "days_back": days_back,
        "daily_send_target": DAILY_TARGET,
        "total_sent": totals["sent"],
        "vs_target": f"{totals['sent']}/{DAILY_TARGET}",
        "open_rate": _pct(totals["opened"], totals["sent"]),
        "reply_rate": _pct(totals["replied"], totals["sent"]),
        "bounce_rate": _pct(totals["bounced"], totals["sent"]),
        "sends_by_mailbox": {
            _label_mailbox(k): v for k, v in sends_by_mailbox.items()
        },
        "by_sequence": [
            {
                **s,
                "open_rate": _pct(s["opened"], s["sent"]),
                "reply_rate": _pct(s["replied"], s["sent"]),
            }
            for s in by_sequence.values()
        ],
        "mailbox_health": accounts if not isinstance(accounts, dict) or "error" not in accounts else {"error": accounts.get("error")},
        "sequences_active": [
            s for s in (seqs if isinstance(seqs, list) else [])
            if isinstance(s, dict) and s.get("active")
        ][:10],
    }
    return digest


# ─── Sequences (emailer_campaigns) ────────────────────────────────────────

@mcp.tool
async def apollo_sequences_list(per_page: int = 50) -> list | dict:
    """List all email sequences with label + active state + top-level stats.

    Uses POST /emailer_campaigns/search (GET /emailer_campaigns creates an empty
    sequence — a dangerous Apollo API quirk).
    """
    data = await _request(
        "POST",
        "/emailer_campaigns/search",
        body={"page": 1, "per_page": per_page},
        cache_key=f"seqs_list_{per_page}",
    )
    if "error" in data:
        return data
    seqs = data.get("emailer_campaigns") or data.get("results") or []
    return [
        {
            "id": s.get("id"),
            "label": _label_sequence(s.get("id", ""), s.get("name") or ""),
            "name": s.get("name"),
            "active": s.get("active"),
            "archived": s.get("archived"),
            "num_steps": s.get("num_steps"),
            "unique_scheduled": s.get("unique_scheduled"),
            "unique_delivered": s.get("unique_delivered"),
            "unique_opened": s.get("unique_opened"),
            "unique_replied": s.get("unique_replied"),
            "unique_bounced": s.get("unique_bounced") or s.get("bounced"),
            "num_contacts": s.get("num_contacts"),
            "user_id": s.get("user_id"),
            "created_at": s.get("created_at"),
        }
        for s in seqs
    ]


@mcp.tool
async def apollo_sequence_get(sequence_id: str) -> dict:
    """Detailed sequence info including steps and stats."""
    return await _request("GET", f"/emailer_campaigns/{sequence_id}")


@mcp.tool
async def apollo_sequence_add_contacts(
    sequence_id: str,
    contact_ids: list[str],
    send_email_from_email_account_id: Optional[str] = None,
) -> dict:
    """Enroll contacts into a sequence. contact_ids are Apollo contact IDs."""
    body = {
        "contact_ids": contact_ids,
        "emailer_campaign_id": sequence_id,
    }
    if send_email_from_email_account_id:
        body["send_email_from_email_account_id"] = send_email_from_email_account_id
    return await _request("POST", f"/emailer_campaigns/{sequence_id}/add_contact_ids", body=body)


@mcp.tool
async def apollo_sequence_remove_contacts(sequence_id: str, contact_ids: list[str]) -> dict:
    """Remove contacts from a sequence.

    Apollo has no working sequence-scoped unenroll endpoint:
      - POST /emailer_campaigns/{id}/remove_contact_ids -> 404
      - POST /emailer_campaigns/remove_contact_ids      -> 200 but no-op
    The only reliable way to stop future sends is to delete the contact with
    DELETE /contacts/{id}, which cascades and removes all campaign enrollments.
    Destructive: the contact record itself is marked deleted.
    Verified 2026-04-17."""
    results = []
    for cid in contact_ids:
        r = await _request("DELETE", f"/contacts/{cid}")
        results.append({"contact_id": cid, "deleted": (r.get("contact") or {}).get("deleted", False)})
    return {"removed": results, "note": "contacts deleted (only working unenroll path)"}


@mcp.tool
async def apollo_sequence_set_active(sequence_id: str, active: bool) -> dict:
    """Activate or pause a sequence (active=True to activate, False to pause)."""
    return await _request(
        "PUT",
        f"/emailer_campaigns/{sequence_id}",
        body={"emailer_campaign": {"active": active}},
    )


# ─── Messages (the underlying send/open/reply log) ────────────────────────

@mcp.tool
async def apollo_messages_search(
    days_back: int = 1,
    sequence_ids: Optional[list[str]] = None,
    stats: Optional[list[str]] = None,
    per_page: int = 100,
    max_pages: int = 10,
) -> dict:
    """Search the emailer_messages log.

    Args:
        days_back: Window in days from today. Default 1 (last 24h).
        sequence_ids: Filter by sequence IDs.
        stats: Filter by status tokens like ["delivered", "opened", "replied", "bounced"].
        per_page: Results per page (max 100).
        max_pages: Hard cap to prevent runaway pagination.
    """
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    date_min = (now - timedelta(days=days_back)).strftime("%Y-%m-%d")
    date_max = now.strftime("%Y-%m-%d")

    body: dict = {
        "emailer_message_date_range": {"min": date_min, "max": date_max},
    }
    if sequence_ids:
        body["emailer_campaign_ids"] = sequence_ids
    if stats:
        body["emailer_message_stats"] = stats

    messages = await _paginate(
        "POST",
        "/emailer_messages/search",
        body=body,
        key="emailer_messages",
        per_page=per_page,
        max_pages=max_pages,
    )
    return {
        "count": len(messages),
        "date_range": {"min": date_min, "max": date_max},
        "emailer_messages": messages,
    }


# ─── Email accounts (mailbox warmup/health) ───────────────────────────────

@mcp.tool
async def apollo_mailbox_warmup() -> list | dict:
    """Per-mailbox state: daily limit, sent today, warmup indicators."""
    data = await _request("GET", "/email_accounts", cache_key="email_accounts")
    if "error" in data:
        return data
    accounts = data.get("email_accounts") or data.get("results") or []
    return [
        {
            "email": a.get("email"),
            "label": _label_mailbox(a.get("email", "")),
            "provider": a.get("provider"),
            "active": a.get("active"),
            "daily_send_limit": a.get("daily_send_limit") or a.get("daily_limit"),
            "sent_today": a.get("sent_today") or a.get("emails_sent_today"),
            "warmup_status": a.get("warmup_status") or a.get("warmup_state"),
            "default": a.get("default"),
            "id": a.get("id"),
        }
        for a in accounts
    ]


# ─── People + org search/enrichment ───────────────────────────────────────

@mcp.tool
async def apollo_people_search(
    q_keywords: Optional[str] = None,
    person_titles: Optional[list[str]] = None,
    person_seniorities: Optional[list[str]] = None,
    person_locations: Optional[list[str]] = None,
    organization_domains: Optional[list[str]] = None,
    per_page: int = 25,
    page: int = 1,
) -> dict:
    """Search Apollo people database. Does NOT burn credits (search is free; reveal costs)."""
    body: dict = {"page": page, "per_page": per_page}
    if q_keywords: body["q_keywords"] = q_keywords
    if person_titles: body["person_titles"] = person_titles
    if person_seniorities: body["person_seniorities"] = person_seniorities
    if person_locations: body["person_locations"] = person_locations
    if organization_domains: body["q_organization_domains_list"] = organization_domains
    return await _request("POST", "/mixed_people/search", body=body)


@mcp.tool
async def apollo_person_enrich(
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    email: Optional[str] = None,
    domain: Optional[str] = None,
    organization_name: Optional[str] = None,
    linkedin_url: Optional[str] = None,
    reveal_personal_emails: bool = False,
) -> dict:
    """Enrich a single person. BURNS 1 credit if reveal_personal_emails=True."""
    body: dict = {}
    if first_name: body["first_name"] = first_name
    if last_name: body["last_name"] = last_name
    if email: body["email"] = email
    if domain: body["domain"] = domain
    if organization_name: body["organization_name"] = organization_name
    if linkedin_url: body["linkedin_url"] = linkedin_url
    if reveal_personal_emails: body["reveal_personal_emails"] = True
    return await _request("POST", "/people/match", body=body)


@mcp.tool
async def apollo_bulk_person_enrich(
    people: list[dict],
    reveal_personal_emails: bool = False,
) -> dict:
    """Bulk enrich up to 10 people per call. Each BURNS 1 credit if revealing emails."""
    body = {"details": people[:10]}
    if reveal_personal_emails:
        body["reveal_personal_emails"] = True
    return await _request("POST", "/people/bulk_match", body=body)


@mcp.tool
async def apollo_org_search(
    q_keywords: Optional[str] = None,
    organization_locations: Optional[list[str]] = None,
    organization_num_employees_ranges: Optional[list[str]] = None,
    per_page: int = 25,
    page: int = 1,
) -> dict:
    """Search organizations."""
    body: dict = {"page": page, "per_page": per_page}
    if q_keywords: body["q_keywords"] = q_keywords
    if organization_locations: body["organization_locations"] = organization_locations
    if organization_num_employees_ranges:
        body["organization_num_employees_ranges"] = organization_num_employees_ranges
    return await _request("POST", "/mixed_companies/search", body=body)


@mcp.tool
async def apollo_org_enrich(domain: str) -> dict:
    """Enrich an organization by domain."""
    return await _request("GET", "/organizations/enrich", params={"domain": domain})


@mcp.tool
async def apollo_org_job_postings(organization_id: str) -> dict:
    """Get current job postings for an organization."""
    return await _request("GET", f"/organizations/{organization_id}/job_postings")


# ─── CRM contacts + accounts ──────────────────────────────────────────────

@mcp.tool
async def apollo_contacts_search(
    q_keywords: Optional[str] = None,
    contact_stage_ids: Optional[list[str]] = None,
    per_page: int = 25,
    page: int = 1,
) -> dict:
    """Search contacts already in your Apollo CRM."""
    body: dict = {"page": page, "per_page": per_page}
    if q_keywords: body["q_keywords"] = q_keywords
    if contact_stage_ids: body["contact_stage_ids"] = contact_stage_ids
    return await _request("POST", "/contacts/search", body=body)


@mcp.tool
async def apollo_contact_create(
    first_name: str,
    last_name: str,
    email: Optional[str] = None,
    title: Optional[str] = None,
    organization_name: Optional[str] = None,
    label_names: Optional[list[str]] = None,
) -> dict:
    """Create a new contact in Apollo CRM."""
    body: dict = {"first_name": first_name, "last_name": last_name}
    if email: body["email"] = email
    if title: body["title"] = title
    if organization_name: body["organization_name"] = organization_name
    if label_names: body["label_names"] = label_names
    return await _request("POST", "/contacts", body=body)


@mcp.tool
async def apollo_contact_update(contact_id: str, fields: dict) -> dict:
    """Update a contact by ID. fields keys: title, contact_stage_id, email, label_names, etc."""
    return await _request("PUT", f"/contacts/{contact_id}", body=fields)


@mcp.tool
async def apollo_contact_stages() -> dict:
    """List available contact pipeline stages."""
    return await _request("GET", "/contact_stages", cache_key="contact_stages")


# ─── Tasks ────────────────────────────────────────────────────────────────

@mcp.tool
async def apollo_task_create(
    user_id: str,
    contact_ids: list[str],
    priority: str = "medium",
    due_at: Optional[str] = None,
    type_: str = "call",
    note: Optional[str] = None,
) -> dict:
    """Create a task in Apollo. priority=low|medium|high. type=call|action_item|linkedin|outreach_manual_email."""
    body = {
        "user_id": user_id,
        "contact_ids": contact_ids,
        "priority": priority,
        "type": type_,
    }
    if due_at: body["due_at"] = due_at
    if note: body["note"] = note
    return await _request("POST", "/tasks/bulk_create", body=body)


# ─── Credits / usage tracking ─────────────────────────────────────────────

@mcp.tool
async def apollo_credits_remaining() -> dict:
    """Credit consumption for the current billing period. Alerts past 75% usage."""
    data = await _request("GET", "/usage_stats/api_usage_stats")
    if "error" in data:
        # Some accounts use /auth/health or /users/me for credit state
        alt = await _request("GET", "/auth/health")
        return {"error": data.get("error"), "fallback": alt}
    used = data.get("credits_used") or 0
    remaining = data.get("credits_remaining") or (CREDIT_POOL - used)
    pct_used = used / CREDIT_POOL if CREDIT_POOL else 0
    return {
        "monthly_pool": CREDIT_POOL,
        "used": used,
        "remaining": remaining,
        "pct_used": round(pct_used, 3),
        "alert": pct_used >= CREDIT_ALERT_PCT,
        "raw": data,
    }


# ─── Labels + lists ───────────────────────────────────────────────────────

@mcp.tool
async def apollo_labels_list() -> dict:
    """List all contact labels."""
    return await _request("GET", "/labels", cache_key="labels")


@mcp.tool
async def apollo_label_create(name: str) -> dict:
    """Create a new label."""
    return await _request("POST", "/labels", body={"name": name})


# ─── Diagnostics ──────────────────────────────────────────────────────────

@mcp.tool
async def apollo_health() -> dict:
    """Server self-check: API key present, master-key hint, basic connectivity."""
    result = {
        "api_key_set": bool(API_KEY),
        "api_key_prefix": (API_KEY[:6] + "..." if API_KEY else None),
        "api_base": API_BASE,
        "config_path": str(_CFG_PATH),
        "config_loaded": bool(CONFIG),
        "sequence_labels_count": len(SEQUENCE_LABELS),
        "mailbox_labels_count": len(MAILBOX_LABELS),
    }
    if API_KEY:
        probe = await _request("GET", "/auth/health")
        result["auth_probe"] = probe
        seqs_probe = await _request(
            "POST", "/emailer_campaigns/search", body={"page": 1, "per_page": 1}
        )
        if "error" not in seqs_probe:
            result["master_key_probe"] = "likely_master"
            result["visible_sequences"] = (seqs_probe.get("pagination") or {}).get("total_entries")
        else:
            result["master_key_probe"] = f"probably_not_master: {seqs_probe.get('error')}"
    return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not API_KEY:
        print("WARNING: APOLLO_API_KEY not set. Server will start but all calls will fail.", file=sys.stderr)
    mcp.run()
