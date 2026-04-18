# Apollo.io MCP Server

FastMCP server exposing the full operational surface of the Apollo.io REST API to Claude Code. 22 tools covering sequences, campaign health, mailbox warmup, people/org enrichment, CRM contacts, tasks, labels, and credit tracking.

Most existing Apollo MCPs only expose people search and enrichment. This one is built for teams running outbound at scale — daily health digests, sequence management, mailbox deliverability monitoring, and programmatic contact enrollment.

## Quick reference

```
apollo_campaign_health                — daily digest (start here)
apollo_messages_search days_back=1    — raw send/open/reply/bounce log
apollo_mailbox_warmup                 — per-mailbox deliverability state
apollo_sequences_list                 — all sequences + labels + stats
apollo_sequence_add_contacts ...      — enroll contacts into a sequence
apollo_credits_remaining              — monthly credit budget check
apollo_health                         — self-check (also probes master key)
```

## Tool inventory (22)

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

## Install

```bash
pip3 install -r requirements.txt
python3 -c "import server; print('OK')"
```

## Register in Claude Code

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

## License

MIT
