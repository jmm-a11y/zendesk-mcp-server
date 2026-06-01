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
| `search_tickets` | Search via Zendesk Search API — use this for filtered queries (unassigned, by status, by requester, etc.) |
| `get_tickets` | Paginated list of all tickets — use only when you need a broad dump |
| `get_ticket` | Single ticket by ID |
| `create_ticket` | Create a new ticket |
| `update_ticket` | Update ticket fields (status, priority, assignee_id, etc.) |
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

## Build and deploy

The server runs in Docker and is consumed by Claude Desktop via stdio.

```bash
# Build
docker build -t zendesk-mcp-server .

# Run standalone (for testing)
docker run --env-file .env zendesk-mcp-server
```

After any code change: rebuild the image, then restart Claude Desktop so it reconnects to the updated container.

## Development

Requires Python 3.12+. Dependencies managed with `uv`.

```bash
uv sync
uv run zendesk   # run server locally (reads .env)
```
