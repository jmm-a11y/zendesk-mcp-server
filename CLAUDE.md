# Zendesk MCP Server

MCP server that exposes Zendesk ticket operations to Claude. Built and maintained by Techsourcing.

## What it does

Provides Claude with tools to read, create, update, search, and merge Zendesk tickets, post comments, fetch attachments, and look up Techsourcing staff by name or alias.

## Project layout

```
src/zendesk_mcp_server/
  server.py          # MCP server — tool definitions and handlers
  zendesk_client.py  # Zendesk API wrapper (zenpy + direct REST)
  techs.json         # Techsourcing staff roster for find_tech tool
```

## Tools

| Tool | Purpose |
|------|---------|
| `search_tickets` | Search via Zendesk Search API — results include `organization_id`; use this for filtered queries (unassigned, by status, by requester, etc.) |
| `get_tickets` | Paginated list of all tickets — use only when you need a broad dump |
| `get_ticket` | Single ticket by ID — returns `collaborators` and `email_ccs` (resolved to name/email); use `include_comments=true` when evaluating a merge target |
| `lookup_user` | Resolve any email address to a Zendesk user ID — use before `create_ticket` |
| `create_ticket` | Create a new ticket — always call `lookup_user` first to set `requester_id` |
| `get_custom_statuses` | List all custom statuses with IDs — use to find the right `custom_status_id` |
| `list_organizations` | Return all orgs with `id`, `name`, `domain_names` — call once to build an id→name map for resolving `organization_id` from search results |
| `find_merge_candidates` | Find standing monitoring tickets for new unassigned alerts — returns each new ticket paired with candidates |
| `update_ticket` | Update ticket fields — supports `custom_status_id`, `collaborator_ids`, `email_cc_ids`; CC fields are full-replace (read current list first, then write complete list) |
| `upload_file` | Upload a file and return an attachment token — accepts `local_path` (disk) or `file_bytes` + `filename` (base64, for chat-uploaded images) |
| `get_ticket_comments` | All comments on a ticket, including attachment metadata |
| `create_ticket_comment` | Post a public or internal comment — use HTML, not markdown |
| `get_ticket_attachment` | Fetch an image attachment as base64 |
| `merge_tickets` | Merge duplicate tickets (returns async job) |
| `get_job_status` | Poll a merge job by job ID |
| `find_tech` | Look up a Techsourcing tech by name, alias, or email — returns `zendesk_user_id` for use with `update_ticket` |

### search_tickets — preferred over get_tickets for filtering

Use Zendesk search syntax in the `query` parameter:
- `type:ticket assignee:none status:open` — all unassigned open tickets
- `type:ticket status:open` — all open tickets
- `type:ticket requester:user@example.com` — by requester
- `type:ticket created>2024-01-01` — by date

`get_tickets` sorts by `updated_at desc` and requires manual pagination; stale tickets (untouched for weeks) can be missed. `search_tickets` queries server-side and returns complete results.

## Custom ticket statuses

To set a custom status on a ticket:

1. Call `get_custom_statuses` to find the right `id` (e.g. "Event Scheduled" → some integer)
2. Call `update_ticket` with both `custom_status_id` and the base `status` field (the status category the custom status belongs to — e.g. `status=pending` for a pending-category custom status). Passing both is explicit and avoids ambiguity when similar custom statuses exist across categories (e.g. "Open (Monitoring)" vs "Pending (Monitoring)").

## Attaching files to comments

`upload_file` supports two modes:

**Local file (e.g. a Word doc built in Claude):**
1. Call Foundation's `list_downloads` to confirm the exact local path
2. Call `upload_file(local_path=...)` — returns a `token` and `expires_at`
3. Immediately call `create_ticket_comment` with `uploads: [token]`

**Chat-uploaded image or sandbox file:**
1. Encode the file content as standard base64 (no data-URI prefix)
2. Call `upload_file(file_bytes=..., filename="screenshot.png")` — returns a `token`
3. Immediately call `create_ticket_comment` with `uploads: [token]`

Tokens expire after 60 minutes — upload and comment in the same turn. Content-Type is inferred from the filename extension automatically.

## Creating tickets — requester workflow

Always resolve the requester before creating a ticket:

1. Call `lookup_user` with the requester's email → get their Zendesk user ID
2. Pass that ID as `requester_id` to `create_ticket`
3. After creation, confirm the returned `requester_id` matches the expected user — if Zendesk defaulted it to the API caller, call `update_ticket` to correct it

If `lookup_user` returns `found: false`, stop and flag it — do not create the ticket with no requester, as it will be silently attributed to the API caller (JMM).

## Comment formatting

Zendesk does not render markdown in API-posted comments. Always use HTML for structured content:

- `<p>` for paragraph breaks
- `<b>` for bold / section headings
- `<ul>` / `<ol>` / `<li>` for lists

Plain prose with no formatting needs no tags. Never use markdown syntax (`**bold**`, `- list`, `# heading`, etc.) in comment bodies — it appears as raw characters in the agent UI and in client-facing replies.

## techs.json — staff roster

`src/zendesk_mcp_server/techs.json` maps Techsourcing staff to their Zendesk user IDs. Used by `find_tech`.

```json
{
  "techs": [
    {
      "zendesk_user_id": 123456789,
      "name": "Full Name",
      "email": "user@techsourcing.com",
      "aliases": ["shortname", "initials"]
    }
  ]
}
```

To add or update a tech, edit this file directly. No code changes needed.

## Environment variables

```
ZENDESK_SUBDOMAIN=yoursubdomain
ZENDESK_EMAIL=your@email.com
ZENDESK_API_KEY=your_api_token
```

Copy `.env.example` to `.env` and fill in the values. The `.env` file is gitignored.

## Claude Desktop config

The server runs as a local `uv` process — same pattern as Foundation and Billing.

```json
"zendesk": {
  "command": "C:\\Users\\JohnMMoore\\.local\\bin\\uv.exe",
  "args": [
    "run",
    "--directory", "C:\\Users\\JohnMMoore\\dev\\zendesk-mcp-server",
    "python", "C:\\Users\\JohnMMoore\\dev\\zendesk-mcp-server\\src\\zendesk_mcp_server\\server.py"
  ]
}
```

After any code change, restart Claude Desktop to pick up the latest server code.

## Development

Requires Python 3.12+. Dependencies managed with `uv`.

```bash
uv sync
uv run zendesk   # run server locally (reads .env)
```
