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
    """Activate or pause a sequence (active=True to activate, False to pause).

    Uses PATCH + flat body, matching the working pattern in
    apollo_template_update and apollo_mailbox_update_cap. The prior
    PUT + nested-wrap pattern silently no-op'd in production — the same
    antipattern that apollo_template_update's own comments document for
    /emailer_templates.

    Post-write verify: re-fetches the campaign after the PATCH and compares
    the active flag. Surfaces a _warning if Apollo accepted the request but
    didn't apply the change (which may indicate a separate UI-only
    activation constraint for cold-outreach campaigns).
    """
    pre = await _request("GET", f"/emailer_campaigns/{sequence_id}")
    if "error" in pre:
        return {"error": "Pre-update snapshot failed", "detail": pre}
    pre_active = (pre.get("emailer_campaign") or pre).get("active")

    result = await _request(
        "PATCH",
        f"/emailer_campaigns/{sequence_id}",
        body={"active": active},
    )
    if "error" in result:
        return {"error": "Update failed", "detail": result}

    # Post-write verify (mirrors apollo_template_update's verification step).
    verify = await _request("GET", f"/emailer_campaigns/{sequence_id}")
    verified_active = (verify.get("emailer_campaign") or {}).get("active")
    write_landed = verified_active == active

    response: dict = {
        "sequence_id": sequence_id,
        "requested_active": active,
        "before_active": pre_active,
        "after_active": verified_active,
        "updated": write_landed,
        "silent_no_op": not write_landed,
    }
    if not write_landed:
        response["_warning"] = (
            f"Requested active={active} but Apollo returned active={verified_active}. "
            "Apollo may enforce UI-only activation for cold-outreach campaigns "
            "(anti-abuse). Flip the active toggle in the Apollo UI as a workaround."
        )
    return response


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
    """Search Apollo people database. Does NOT burn credits (search is free; reveal costs).

    Uses /mixed_people/api_search (Apollo deprecated /mixed_people/search for
    API callers in 2026 with HTTP 422). Verified 2026-05-11.
    """
    body: dict = {"page": page, "per_page": per_page}
    if q_keywords: body["q_keywords"] = q_keywords
    if person_titles: body["person_titles"] = person_titles
    if person_seniorities: body["person_seniorities"] = person_seniorities
    if person_locations: body["person_locations"] = person_locations
    if organization_domains: body["q_organization_domains_list"] = organization_domains
    return await _request("POST", "/mixed_people/api_search", body=body)


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


# ─── Audit log helper ─────────────────────────────────────────────────────

async def _write_audit_log(
    action: str,
    target_id: str,
    label: Optional[str],
    before: dict,
    after: dict,
    path: str,
) -> str:
    """Append a destructive-write entry to the audit log file at `path`.

    Format: markdown entry with timestamp + before/after JSON diff. Idempotent
    (always appends; never rewrites prior entries). If the file does not exist,
    seeds it with a frontmatter header.

    Returns a human-readable summary string for inclusion in tool responses.
    """
    from datetime import datetime

    audit_file = Path(path)
    audit_file.parent.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().isoformat()
    lines = [f"## {timestamp} — {action} — {target_id}", ""]
    if label:
        lines += [f"**Label:** {label}", ""]
    lines += [
        "**Before:**",
        "```json",
        json.dumps(before, indent=2, ensure_ascii=False),
        "```",
        "",
        "**After:**",
        "```json",
        json.dumps(after, indent=2, ensure_ascii=False),
        "```",
        "",
        "---",
        "",
    ]
    entry = "\n".join(lines)

    if not audit_file.exists():
        header = (
            "---\n"
            "type: audit-log\n"
            f"creationDate: {timestamp[:10]}\n"
            "purpose: Destructive-write audit trail for apollo-mcp. Append-only.\n"
            "---\n\n"
            "# Apollo MCP Audit Log\n\n"
            "Every destructive write through apollo-mcp lands here. Use the before/after diffs to reverse breakage.\n\n"
            "---\n\n"
        )
        audit_file.write_text(header + entry, encoding="utf-8")
    else:
        with open(audit_file, "a", encoding="utf-8") as f:
            f.write(entry)

    return f"appended to {path}"


def _html_from_text(body_text: str) -> str:
    """Wrap a plain-text body in minimal HTML matching Apollo's template style."""
    paragraphs = [p.strip() for p in body_text.split("\n\n") if p.strip()]
    return (
        "<html><head></head><body>"
        + "".join(f"<p>{p.replace(chr(10), '<br>')}</p>" for p in paragraphs)
        + "</body></html>"
    )


# ─── Template management (destructive writes) ─────────────────────────────

@mcp.tool
async def apollo_template_get(template_id: str) -> dict:
    """Get an emailer template by ID. Use to audit current state before update.

    Args:
        template_id: The emailer_template ID (NOT the step or touch ID).
    """
    return await _request("GET", f"/emailer_templates/{template_id}")


@mcp.tool
async def apollo_template_update(
    template_id: str,
    subject: Optional[str] = None,
    body_text: Optional[str] = None,
    body_html: Optional[str] = None,
    audit_label: Optional[str] = None,
) -> dict:
    """Update an emailer template subject and/or body. Destructive write.

    Snapshots the current template to APOLLO_AUDIT_LOG_PATH (if set) BEFORE the
    update, so any breakage is reversible from the audit log diff.

    Args:
        template_id: The emailer_template ID.
        subject: New subject line. Leave None to keep current.
        body_text: New plain-text body. Leave None to keep current.
        body_html: New HTML body. Auto-generated from body_text if None and
            body_text is provided.
        audit_label: Optional human label for the audit log entry.
    """
    pre = await _request("GET", f"/emailer_templates/{template_id}")
    if "error" in pre:
        return {"error": "Pre-update snapshot failed", "detail": pre}
    pre_t = pre.get("emailer_template") or pre

    update_fields: dict = {}
    if subject is not None:
        update_fields["subject"] = subject
    if body_text is not None:
        update_fields["body_text"] = body_text
        if body_html is None:
            body_html = _html_from_text(body_text)
    if body_html is not None:
        update_fields["body_html"] = body_html

    if not update_fields:
        return {"error": "No fields provided. Pass subject, body_text, or body_html."}

    audit_path = os.environ.get("APOLLO_AUDIT_LOG_PATH", "").strip()
    audit_log_entry = None
    if audit_path:
        try:
            audit_log_entry = await _write_audit_log(
                action="template_update",
                target_id=template_id,
                label=audit_label,
                before={
                    "subject": pre_t.get("subject"),
                    "body_text": pre_t.get("body_text"),
                },
                after=update_fields,
                path=audit_path,
            )
        except Exception as e:
            audit_log_entry = f"audit_log_write_failed: {type(e).__name__}: {e}"

    # Apollo's template-update endpoint is PATCH + flat body. PUT + nested wrap
    # returns 200 OK but silently no-ops (the same antipattern as the
    # /emailer_campaigns remove_contact_ids endpoint). Verified 2026-05-11.
    result = await _request(
        "PATCH",
        f"/emailer_templates/{template_id}",
        body=update_fields,
    )
    if "error" in result:
        return {
            "error": "Update failed",
            "detail": result,
            "audit_log_entry": audit_log_entry,
        }

    # Post-write verify: re-fetch and confirm the subject/body actually changed.
    # Apollo can still 200-no-op on malformed bodies, so we trust GET not the PUT response.
    verify = await _request("GET", f"/emailer_templates/{template_id}")
    verified_t = verify.get("emailer_template") or {}
    write_landed = True
    if subject is not None and verified_t.get("subject") != subject:
        write_landed = False
    if body_text is not None and verified_t.get("body_text") != body_text:
        write_landed = False

    return {
        "updated": write_landed,
        "silent_no_op": not write_landed,
        "template_id": template_id,
        "fields_updated": list(update_fields.keys()),
        "verified_subject": verified_t.get("subject"),
        "audit_log_entry": audit_log_entry,
    }


# ─── Sequence + step creation (destructive writes) ────────────────────────

@mcp.tool
async def apollo_sequence_create(
    name: str,
    label: Optional[str] = None,
    permissions: str = "team_can_use",
    active: bool = False,
) -> dict:
    """Create a new email sequence (emailer_campaign). Starts PAUSED by default.

    Use apollo_step_create to add steps. Use apollo_sequence_set_active(True)
    to launch when ready.

    Args:
        name: Sequence name shown in Apollo UI.
        label: Optional cross-reference label (echoed in response only).
        permissions: "team_can_use" | "private" | "team_can_view_and_edit".
        active: Start active. Default False so steps can be built first.
    """
    # Apollo's POST /emailer_campaigns wants flat body (not nested wrap).
    body = {
        "name": name,
        "permissions": permissions,
        "active": active,
    }
    result = await _request("POST", "/emailer_campaigns", body=body)
    if "error" in result:
        return result

    seq = result.get("emailer_campaign") or result
    return {
        "created": True,
        "sequence_id": seq.get("id"),
        "name": seq.get("name"),
        "active": seq.get("active"),
        "label": label,
        "sequence": seq,
    }


@mcp.tool
async def apollo_step_create(
    sequence_id: str,
    position: int,
    wait_days: int,
    subject: str,
    body_text: str,
    body_html: Optional[str] = None,
    step_type: str = "auto_email",
    include_signature: bool = True,
) -> dict:
    """Create a sequence step plus its initial touch and template in one call.

    Args:
        sequence_id: The emailer_campaign ID.
        position: 1-based position in the sequence.
        wait_days: Days to wait BEFORE this step fires (position 1 typically 0).
        subject: Email subject line.
        body_text: Plain-text body.
        body_html: Optional HTML body. Auto-generated from body_text if None.
        step_type: "auto_email" (default) or "manual_email".
        include_signature: Append the user's saved signature.
    """
    # Apollo wants flat bodies on these endpoints (nested wrap silently no-ops).
    # POST /emailer_steps returns BOTH emailer_step AND emailer_touch with auto-
    # created template — we just need to PATCH the template body afterwards.
    step_body = {
        "emailer_campaign_id": sequence_id,
        "position": position,
        "wait_time": wait_days,
        "wait_mode": "day",
        "type": step_type,
    }
    step_result = await _request("POST", "/emailer_steps", body=step_body)
    if "error" in step_result:
        return {"error": "Step create failed", "detail": step_result}
    step = step_result.get("emailer_step") or step_result
    step_id = step.get("id")
    touch = step_result.get("emailer_touch") or {}
    touch_id = touch.get("id")
    template_id = touch.get("emailer_template_id")

    if not template_id:
        return {
            "error": "Step created but no template_id returned",
            "step_id": step_id,
            "touch_id": touch_id,
        }

    if body_html is None:
        body_html = _html_from_text(body_text)

    # PATCH the auto-created template with the real content.
    tpl_update = await _request(
        "PATCH",
        f"/emailer_templates/{template_id}",
        body={"subject": subject, "body_text": body_text, "body_html": body_html},
    )
    if "error" in tpl_update:
        return {
            "error": "Template update failed (step+touch were created)",
            "step_id": step_id,
            "touch_id": touch_id,
            "template_id": template_id,
            "detail": tpl_update,
        }

    # Approve the touch so the step actually fires. PATCH on the touch returns
    # 422; this action endpoint is the only way to approve programmatically.
    approve = await _request("POST", f"/emailer_touches/{touch_id}/approve")
    approved = "error" not in approve

    return {
        "created": True,
        "step_id": step_id,
        "touch_id": touch_id,
        "template_id": template_id,
        "position": position,
        "wait_days": wait_days,
        "subject": subject,
        "approved": approved,
        "approve_detail": approve if not approved else None,
    }


# ─── Mailbox cap update (destructive write) ───────────────────────────────

@mcp.tool
async def apollo_mailbox_update_cap(
    mailbox_id: str,
    daily_send_limit: int,
    audit_label: Optional[str] = None,
) -> dict:
    """Update the daily send cap on a mailbox.

    Apollo's daily_send_limit gates how many emails a mailbox sends per day.
    Increase only when warmup status is healthy and deliverability supports it.

    Args:
        mailbox_id: The email_account ID (NOT the email address).
        daily_send_limit: New cap (per-day integer).
        audit_label: Optional human label for the audit log.
    """
    pre = await _request("GET", f"/email_accounts/{mailbox_id}")
    pre_account = pre.get("email_account") or pre
    pre_cap = pre_account.get("daily_send_limit") or pre_account.get("daily_limit")

    audit_path = os.environ.get("APOLLO_AUDIT_LOG_PATH", "").strip()
    audit_log_entry = None
    if audit_path:
        try:
            audit_log_entry = await _write_audit_log(
                action="mailbox_update_cap",
                target_id=mailbox_id,
                label=audit_label,
                before={"daily_send_limit": pre_cap},
                after={"daily_send_limit": daily_send_limit},
                path=audit_path,
            )
        except Exception as e:
            audit_log_entry = f"audit_log_write_failed: {type(e).__name__}: {e}"

    # Apollo's mailbox-update is PATCH + flat body (PUT + nested silently no-ops).
    result = await _request(
        "PATCH",
        f"/email_accounts/{mailbox_id}",
        body={"daily_send_limit": daily_send_limit},
    )
    if "error" in result:
        return {
            "error": "Cap update failed",
            "detail": result,
            "audit_log_entry": audit_log_entry,
        }

    # Post-write verify
    verify = await _request("GET", f"/email_accounts/{mailbox_id}")
    verified_account = verify.get("email_account") or {}
    verified_cap = verified_account.get("daily_send_limit") or verified_account.get("daily_limit")
    write_landed = verified_cap == daily_send_limit

    return {
        "updated": write_landed,
        "silent_no_op": not write_landed,
        "mailbox_id": mailbox_id,
        "old_cap": pre_cap,
        "new_cap": verified_cap,
        "audit_log_entry": audit_log_entry,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not API_KEY:
        print("WARNING: APOLLO_API_KEY not set. Server will start but all calls will fail.", file=sys.stderr)
    mcp.run()
