# Apollo.io MCP Server


<!-- mycelium-badges:start -->

<p>
  <a href="https://github.com/adelaidasofia/apollo-mcp/blob/main/LICENSE"><img alt="License" src="https://img.shields.io/github/license/adelaidasofia/apollo-mcp?color=blue"></a>
  <a href="https://github.com/adelaidasofia/apollo-mcp/stargazers"><img alt="GitHub stars" src="https://img.shields.io/github/stars/adelaidasofia/apollo-mcp?color=eab308"></a>
  <a href="https://github.com/adelaidasofia/apollo-mcp/commits/main"><img alt="Last commit" src="https://img.shields.io/github/last-commit/adelaidasofia/apollo-mcp"></a>
  <a href="https://github.com/adelaidasofia/apollo-mcp/issues"><img alt="Open issues" src="https://img.shields.io/github/issues/adelaidasofia/apollo-mcp"></a>
  <a href="https://pypi.org/project/adelaidasofia-apollo-mcp/"><img alt="PyPI version" src="https://img.shields.io/pypi/v/adelaidasofia-apollo-mcp?color=blue&label=pypi"></a>
  <a href="https://pypi.org/project/adelaidasofia-apollo-mcp/"><img alt="PyPI downloads" src="https://img.shields.io/pypi/dm/adelaidasofia-apollo-mcp?color=blue&label=downloads"></a>
  <a href="https://myceliumai.co"><img alt="Built by Mycelium AI" src="https://img.shields.io/badge/built_by-Mycelium_AI-15B89A"></a>
</p>

<!-- mycelium-badges:end -->

FastMCP server exposing the full operational surface of the Apollo.io REST API to Claude Code. 27 tools covering sequences, campaign health, mailbox warmup, people/org enrichment, CRM contacts, tasks, labels, credit tracking, AND programmatic template/sequence/mailbox-cap editing with audit logging.

Most existing Apollo MCPs only expose people search and enrichment. This one is built for teams running outbound at scale — daily health digests, sequence management, mailbox deliverability monitoring, programmatic contact enrollment, and end-to-end sequence authoring without touching the Apollo UI.

## Quick reference

```
apollo_campaign_health                — daily digest (start here)
apollo_messages_search days_back=1    — raw send/open/reply/bounce log
apollo_mailbox_warmup                 — per-mailbox deliverability state
apollo_sequences_list                 — all sequences + labels + stats
apollo_sequence_add_contacts ...      — enroll contacts into a sequence
apollo_template_update ...            — update template body + subject (with audit log)
apollo_sequence_create ...            — author a new sequence end-to-end
apollo_step_create ...                — add a step + template to a sequence
apollo_mailbox_update_cap ...         — change daily send cap
apollo_credits_remaining              — monthly credit budget check
apollo_health                         — self-check (also probes master key)
```

## Tool inventory (27)

### Campaign health
- `apollo_campaign_health(days_back=1)` — digest across all sequences + mailboxes
- `apollo_mailbox_warmup()` — per-mailbox warmup status and daily limits
- `apollo_sequence_get(sequence_id)` — one-sequence deep-dive

### Sequences
- `apollo_sequences_list()` — list all sequences with stats
- `apollo_sequence_add_contacts(sequence_id, contact_ids, send_email_from_email_account_id?)`
- `apollo_sequence_remove_contacts(sequence_id, contact_ids)`
- `apollo_sequence_set_active(sequence_id, active)` — activate or pause

### Messages / analytics
- `apollo_messages_search(days_back, sequence_ids?, stats?, per_page, max_pages)`

### People / orgs
- `apollo_people_search(...)` — free (no credit burn)
- `apollo_person_enrich(...)` — 1 credit per revealed email
- `apollo_bulk_person_enrich(people, reveal_personal_emails?)` — up to 10/call
- `apollo_org_search(...)`
- `apollo_org_enrich(domain)`
- `apollo_org_job_postings(organization_id)`

### CRM
- `apollo_contacts_search(...)`
- `apollo_contact_create(...)`
- `apollo_contact_update(contact_id, fields)`
- `apollo_contact_stages()`

### Tasks / labels / credits
- `apollo_task_create(...)`
- `apollo_labels_list()`
- `apollo_label_create(name)`
- `apollo_credits_remaining()`

### Template + sequence authoring (destructive writes)
- `apollo_template_get(template_id)` — fetch current template state
- `apollo_template_update(template_id, subject?, body_text?, body_html?, audit_label?)` — update template subject + body. Snapshots current state to `APOLLO_AUDIT_LOG_PATH` (if set) BEFORE the write so any breakage is reversible from the diff
- `apollo_sequence_create(name, label?, permissions?, active?)` — create a new sequence (paused by default)
- `apollo_step_create(sequence_id, position, wait_days, subject, body_text, body_html?, step_type?, include_signature?)` — create a step + touch + template in one call
- `apollo_mailbox_update_cap(mailbox_id, daily_send_limit, audit_label?)` — change daily send cap on a mailbox. Audits before/after.

### Diagnostics
- `apollo_health()` — probes API key validity + master-key hint

## Configuration

Copy `config.example.yaml` to `config.yaml` and fill in your values:

```bash
cp config.example.yaml config.yaml
```

`config.yaml` is gitignored. Key fields:

| Field | Default | Description |
|---|---|---|
| `daily_send_target` | 50 | Used in health digest vs-target display |
| `monthly_credit_pool` | 4000 | Alerts when `credit_alert_pct` consumed |
| `sequence_labels` | `{}` | Map sequence IDs to human-readable names |
| `mailbox_labels` | `{}` | Map email addresses to display names |

Env vars:

| Var | Required | Description |
|---|---|---|
| `APOLLO_API_KEY` | Yes | Must be a **MASTER** key |
| `APOLLO_MCP_CONFIG` | No | Override config.yaml path |
| `APOLLO_MCP_TIMEOUT` | No | HTTP timeout in seconds (default 30) |
| `APOLLO_AUDIT_LOG_PATH` | No | Path to a markdown file where destructive writes (template_update, sequence_create, step_create, mailbox_update_cap) append before/after diffs. Required for reversibility on the destructive-write tools. |

## Install

Open Claude Code, paste:

    /plugin marketplace add adelaidasofia/apollo-mcp
    /plugin install apollo-mcp@apollo-mcp

Then set `APOLLO_API_KEY` (must be a master key) in your environment and restart Claude Code.

<details>
<summary>Legacy install</summary>

```bash
pip3 install -r requirements.txt
python3 -c "import server; print('OK')"
```

Add to your `.mcp.json`:

```json
{
  "mcpServers": {
    "apollo": {
      "command": "python3",
      "args": ["/path/to/apollo-mcp/server.py"],
      "env": {
        "APOLLO_API_KEY": "your-master-key-here"
      }
    }
  }
}
```

Restart Claude Code after editing `.mcp.json`.

</details>

## Known gotchas

1. **MASTER key required** for `/emailer_campaigns`, `/emailer_messages/search`, `/email_accounts`, `/usage_stats/*`. Standard keys return 403. `apollo_health` probes for this automatically.

2. **Rate limit** ~60 req/min. Built-in jittered exponential backoff handles 429s transparently.

3. **Pagination** hard-capped at 50 pages × 100/page = 5,000 records per call. Slice by date for deeper queries.

4. **Credit burn** — `apollo_person_enrich(reveal_personal_emails=True)` and the bulk variant each burn 1 credit.

5. **5-minute in-memory cache** for `sequences_list`, `email_accounts`, `labels`, `contact_stages`. Force-refresh by restarting Claude Code.

6. **Cloudflare 1010 on default Python UAs.** apollo.io returns HTTP 403 with body `error code: 1010` when it sees `python-urllib/*` or `python-httpx/*` user-agents. The server spoofs `apollo-mcp/1.0 (curl/8.0.0)` to bypass. If you build any adjacent scripts hitting apollo.io, add the same `User-Agent` header or you'll debug a phantom 403 that looks like a missing master key.

7. **No working sequence-scoped unenroll endpoint.** `POST /emailer_campaigns/{id}/remove_contact_ids` returns 404. The no-ID variant `POST /emailer_campaigns/remove_contact_ids` returns 200 but is a silent no-op regardless of payload. The only reliable unenroll path is `DELETE /contacts/{id}`, which cascades all campaign enrollments. Destructive: the contact record is marked `deleted: true`. `apollo_sequence_remove_contacts` wraps this.

8. **`GET /emailer_campaigns` creates an empty sequence.** Use `POST /emailer_campaigns/search` to list sequences. The server does this correctly — just be careful if you hit the API directly.

9. **Sequence activation uses `PUT`, not a sub-resource.** The correct endpoint to activate or pause a sequence is `PUT /emailer_campaigns/{id}` with body `{"emailer_campaign": {"active": true/false}}`. Sub-resource paths like `/check_contacts` return 404.

## Scripts

- `scripts/daily_digest.py` — standalone script that pulls yesterday's stats and prepends them to a markdown dashboard. Run via cron or manually.
- `scripts/bootstrap_sequences.py` — create sequences programmatically from code. Useful for version-controlling email copy. Includes the undocumented `POST /emailer_touches/{id}/approve` endpoint needed to move steps out of draft state.

## Related MCPs

Same author, same architecture pattern (FastMCP, draft+confirm on writes where applicable, vault auto-export, MIT):

- [slack-mcp](https://github.com/adelaidasofia/slack-mcp) — multi-workspace Slack
- [imessage-mcp](https://github.com/adelaidasofia/imessage-mcp) — macOS iMessage
- [whatsapp-mcp](https://github.com/adelaidasofia/whatsapp-mcp) — WhatsApp via whatsmeow
- [google-workspace-mcp](https://github.com/adelaidasofia/google-workspace-mcp) — Gmail / Calendar / Drive / Docs / Sheets
- [substack-mcp](https://github.com/adelaidasofia/substack-mcp) — Substack writing + analytics
- [luma-mcp](https://github.com/adelaidasofia/luma-mcp) — lu.ma events
- [parse-mcp](https://github.com/adelaidasofia/parse-mcp) — markitdown / Docling / LlamaParse router
- [rescuetime-mcp](https://github.com/adelaidasofia/rescuetime-mcp) — RescueTime productivity data
- [graph-query-mcp](https://github.com/adelaidasofia/graph-query-mcp) — vault knowledge graph queries
- [graph-autotagger-mcp](https://github.com/adelaidasofia/graph-autotagger-mcp) — wikilink suggestions from the graph
- [investor-relations-mcp](https://github.com/adelaidasofia/investor-relations-mcp) — seed-raise pipeline tracker
- [vault-sync-mcp](https://github.com/adelaidasofia/vault-sync-mcp) — bidirectional vault sync


## Telemetry

This plugin sends a single anonymous install signal to `myceliumai.co` the first time it loads in a Claude Code session on a given machine.

**What is sent:**
- Plugin name (e.g. `slack-mcp`)
- Plugin version (e.g. `0.1.0`)

**What is NOT sent:**
- No user identifiers, names, emails, tokens, or API keys
- No file paths, message content, or anything from your work
- No IP address is stored after dedup processing

**Why:** Helps the maintainer know which plugins people actually install, so attention goes to the ones that get used.

**Opt out:** Set the environment variable `MYCELIUM_NO_PING=1` before launching Claude Code. The hook will skip the network call entirely. Already-pinged installs leave a sentinel at `~/.mycelium/onboarded-<plugin>` — delete it if you want to reset state.

## License

MIT

---

Built by [Mycelium AI](https://myceliumai.co). Full install or team version at [diazroa.com](https://diazroa.com).
