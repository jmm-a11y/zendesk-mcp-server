import asyncio
import json
import logging
import os
import pathlib
from typing import Any, Dict

from cachetools.func import ttl_cache
from dotenv import load_dotenv
from mcp.server import InitializationOptions, NotificationOptions
from mcp.server import Server, types
from mcp.server.stdio import stdio_server
from pydantic import AnyUrl

from zendesk_mcp_server.zendesk_client import ZendeskClient

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("zendesk-mcp-server")
logger.info("zendesk mcp server started")

load_dotenv()
zendesk_client = ZendeskClient(
    subdomain=os.getenv("ZENDESK_SUBDOMAIN"),
    email=os.getenv("ZENDESK_EMAIL"),
    token=os.getenv("ZENDESK_API_KEY")
)

server = Server("Zendesk Server")

_TECHS_PATH = pathlib.Path(__file__).parent / "techs.json"
with _TECHS_PATH.open() as _f:
    _TECHS: list[dict] = json.load(_f)["techs"]


def _find_tech(query: str) -> list[dict]:
    q = query.lower().strip()
    results = []
    for tech in _TECHS:
        haystack = [
            tech["name"].lower(),
            tech["email"].lower(),
            *[a.lower() for a in tech.get("aliases", [])],
        ]
        if any(q in h for h in haystack):
            results.append(tech)
    return results


TICKET_ANALYSIS_TEMPLATE = """
You are a helpful Zendesk support analyst. You've been asked to analyze ticket #{ticket_id}.

Please fetch the ticket info and comments to analyze it and provide:
1. A summary of the issue
2. The current status and timeline
3. Key points of interaction

Remember to be professional and focus on actionable insights.
"""

COMMENT_DRAFT_TEMPLATE = """
You are a helpful Zendesk support agent. You need to draft a response to ticket #{ticket_id}.

Please fetch the ticket info, comments and knowledge base to draft a professional and helpful response that:
1. Acknowledges the customer's concern
2. Addresses the specific issues raised
3. Provides clear next steps or ask for specific details need to proceed
4. Maintains a friendly and professional tone
5. Ask for confirmation before commenting on the ticket

The response should be formatted well and ready to be posted as a comment.
"""


@server.list_prompts()
async def handle_list_prompts() -> list[types.Prompt]:
    """List available prompts"""
    return [
        types.Prompt(
            name="analyze-ticket",
            description="Analyze a Zendesk ticket and provide insights",
            arguments=[
                types.PromptArgument(
                    name="ticket_id",
                    description="The ID of the ticket to analyze",
                    required=True,
                )
            ],
        ),
        types.Prompt(
            name="draft-ticket-response",
            description="Draft a professional response to a Zendesk ticket",
            arguments=[
                types.PromptArgument(
                    name="ticket_id",
                    description="The ID of the ticket to respond to",
                    required=True,
                )
            ],
        )
    ]


@server.get_prompt()
async def handle_get_prompt(name: str, arguments: Dict[str, str] | None) -> types.GetPromptResult:
    """Handle prompt requests"""
    if not arguments or "ticket_id" not in arguments:
        raise ValueError("Missing required argument: ticket_id")

    ticket_id = int(arguments["ticket_id"])
    try:
        if name == "analyze-ticket":
            prompt = TICKET_ANALYSIS_TEMPLATE.format(
                ticket_id=ticket_id
            )
            description = f"Analysis prompt for ticket #{ticket_id}"

        elif name == "draft-ticket-response":
            prompt = COMMENT_DRAFT_TEMPLATE.format(
                ticket_id=ticket_id
            )
            description = f"Response draft prompt for ticket #{ticket_id}"

        else:
            raise ValueError(f"Unknown prompt: {name}")

        return types.GetPromptResult(
            description=description,
            messages=[
                types.PromptMessage(
                    role="user",
                    content=types.TextContent(type="text", text=prompt.strip()),
                )
            ],
        )

    except Exception as e:
        logger.error(f"Error generating prompt: {e}")
        raise


@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    """List available Zendesk tools"""
    return [
        types.Tool(
            name="get_ticket",
            description="Retrieve a ticket by ID. Includes custom_status_id (the operative status — map IDs with get_custom_statuses). Use include_comments=true to embed recent comments for triage context.",
            inputSchema={
                "type": "object",
                "properties": {
                    "ticket_id": {
                        "type": "integer",
                        "description": "The ID of the ticket to retrieve"
                    },
                    "include_comments": {
                        "type": "boolean",
                        "description": "When true, embed the most recent comments in the response for triage context. Default false.",
                        "default": False
                    },
                    "comment_limit": {
                        "type": "integer",
                        "description": "Number of recent comments to include (default 5, max 20). Comments are plain-text only — no html_body or attachment content.",
                        "default": 5
                    }
                },
                "required": ["ticket_id"]
            }
        ),
        types.Tool(
            name="create_ticket",
            description="Create a new ticket. Always call lookup_user first and pass the result as requester_id — Zendesk silently defaults to the API caller otherwise. Verify requester_id in the response.",
            inputSchema={
                "type": "object",
                "properties": {
                    "subject": {"type": "string", "description": "Ticket subject"},
                    "description": {"type": "string", "description": "Ticket description"},
                    "requester_id": {"type": "integer"},
                    "assignee_id": {"type": "integer"},
                    "priority": {"type": "string", "description": "low, normal, high, urgent"},
                    "type": {"type": "string", "description": "problem, incident, question, task"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "custom_fields": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["subject", "description"],
            }
        ),
        types.Tool(
            name="get_tickets",
            description="Fetch the latest tickets with pagination support",
            inputSchema={
                "type": "object",
                "properties": {
                    "page": {
                        "type": "integer",
                        "description": "Page number",
                        "default": 1
                    },
                    "per_page": {
                        "type": "integer",
                        "description": "Number of tickets per page (max 100)",
                        "default": 25
                    },
                    "sort_by": {
                        "type": "string",
                        "description": "Field to sort by (created_at, updated_at, priority, status)",
                        "default": "created_at"
                    },
                    "sort_order": {
                        "type": "string",
                        "description": "Sort order (asc or desc)",
                        "default": "desc"
                    }
                },
                "required": []
            }
        ),
        types.Tool(
            name="get_ticket_comments",
            description="All comments on a ticket. Returns plain-text body (no html_body). Use get_ticket_attachment for attachment content.",
            inputSchema={
                "type": "object",
                "properties": {
                    "ticket_id": {
                        "type": "integer",
                        "description": "The ID of the ticket to get comments for"
                    }
                },
                "required": ["ticket_id"]
            }
        ),
        types.Tool(
            name="create_ticket_comment",
            description="Post a comment on a ticket. Use HTML not markdown — Zendesk does not render markdown. Use <p>, <b>, <ul>/<ol>/<li>. Pass upload tokens in the uploads array to attach files.",
            inputSchema={
                "type": "object",
                "properties": {
                    "ticket_id": {
                        "type": "integer",
                        "description": "The ID of the ticket to comment on"
                    },
                    "comment": {
                        "type": "string",
                        "description": "The comment content. Use HTML for any formatting: <p> for paragraphs, <b> for bold, <ul>/<ol>/<li> for lists. Do NOT use markdown (**, *, #, etc.) — it will not render."
                    },
                    "public": {
                        "type": "boolean",
                        "description": "Whether the comment should be public",
                        "default": True
                    },
                    "uploads": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of upload tokens from upload_file to attach to this comment"
                    }
                },
                "required": ["ticket_id", "comment"]
            }
        ),
        types.Tool(
            name="get_ticket_attachment",
            description="Fetch a Zendesk ticket attachment by its content_url and return the file as base64-encoded data. Use the attachment URLs returned by get_ticket_comments.",
            inputSchema={
                "type": "object",
                "properties": {
                    "content_url": {
                        "type": "string",
                        "description": "The content_url of the attachment from get_ticket_comments"
                    }
                },
                "required": ["content_url"]
            }
        ),
        types.Tool(
            name="update_ticket",
            description=(
                "Update ticket fields. When setting custom_status_id, also pass the base status field — use get_custom_statuses to find the right ID. "
                "collaborator_ids and email_cc_ids are full-replace: to add someone, call get_ticket first to get the current list, append the new user ID, then pass the complete list here."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "ticket_id": {"type": "integer", "description": "The ID of the ticket to update"},
                    "subject": {"type": "string"},
                    "status": {"type": "string", "description": "new, open, pending, on-hold, solved, closed"},
                    "priority": {"type": "string", "description": "low, normal, high, urgent"},
                    "type": {"type": "string"},
                    "assignee_id": {"type": "integer"},
                    "requester_id": {"type": "integer"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "custom_fields": {"type": "array", "items": {"type": "object"}},
                    "due_at": {"type": "string", "description": "ISO8601 datetime"},
                    "custom_status_id": {"type": "integer", "description": "Custom status ID — use get_custom_statuses to find the right ID. Pass alongside the base status field (e.g. status=pending) to be explicit about the status category."},
                    "collaborator_ids": {"type": "array", "items": {"type": "integer"}, "description": "Full list of agent CC user IDs (replaces existing list — include all IDs you want to keep)."},
                    "email_cc_ids": {"type": "array", "items": {"type": "integer"}, "description": "Full list of email CC user IDs (replaces existing list — include all IDs you want to keep)."},
                },
                "required": ["ticket_id"]
            }
        ),
        types.Tool(
            name="merge_tickets",
            description="Merge source tickets into a target. Sources are closed; target survives. Returns a job ID — poll with get_job_status.",
            inputSchema={
                "type": "object",
                "properties": {
                    "target_ticket_id": {
                        "type": "integer",
                        "description": "The ticket that survives the merge"
                    },
                    "source_ticket_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "One or more duplicate tickets to merge into the target (these get closed)",
                        "minItems": 1
                    },
                    "target_comment": {
                        "type": "string",
                        "description": "Optional comment added to the target ticket after the merge"
                    },
                    "source_comment": {
                        "type": "string",
                        "description": "Optional comment added to each source ticket before it is closed"
                    }
                },
                "required": ["target_ticket_id", "source_ticket_ids"]
            }
        ),
        types.Tool(
            name="get_job_status",
            description="Poll the status of an async Zendesk background job (e.g. a ticket merge). Use the job_status.id returned by merge_tickets.",
            inputSchema={
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The job status ID returned by an async operation such as merge_tickets"
                    }
                },
                "required": ["job_id"]
            }
        ),
        types.Tool(
            name="search_tickets",
            description=(
                "Search Zendesk tickets using Zendesk search syntax. Prefer over get_tickets for all filtered queries. "
                "Results include custom_status_id alongside base status. "
                "Common: 'type:ticket status:new assignee:none', 'type:ticket status:open', "
                "'type:ticket requester:user@example.com', 'type:ticket updated>2024-01-01'. "
                "Triage rules: (1) use updated> not created> for recent-activity queries; "
                "(2) the merge target for a recurring alert is usually an older open/pending ticket — search all statuses; "
                "(3) call get_ticket with include_comments=true before merging; "
                "(4) custom_status_id is the operative status, not the base status field."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Zendesk search query string, e.g. 'type:ticket assignee:none status:open'"
                    },
                    "page": {
                        "type": "integer",
                        "description": "Page number (default 1)",
                        "default": 1
                    },
                    "per_page": {
                        "type": "integer",
                        "description": "Results per page, max 100 (default 100)",
                        "default": 100
                    }
                },
                "required": ["query"]
            }
        ),
        types.Tool(
            name="list_organizations",
            description=(
                "Return all Zendesk organizations with their id, name, and domain_names. "
                "Call once to build an id→name map for resolving organization_id values from search_tickets results."
            ),
            inputSchema={"type": "object", "properties": {}}
        ),
        types.Tool(
            name="find_tech",
            description="Look up a Techsourcing staff member by name, alias, or email. Returns zendesk_user_id for use as assignee_id.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Name fragment, alias, or email to search for (e.g. 'john', 'jmm', 'akhan@techsourcing.com')"
                    }
                },
                "required": ["query"]
            }
        ),
        types.Tool(
            name="lookup_user",
            description="Look up any Zendesk user by email. Use before create_ticket to get requester_id. Returns id, name, email, role if found; found=false if not.",
            inputSchema={
                "type": "object",
                "properties": {
                    "email": {
                        "type": "string",
                        "description": "The email address to look up"
                    }
                },
                "required": ["email"]
            }
        ),
        types.Tool(
            name="get_custom_statuses",
            description="List all custom ticket statuses (id, agent_label, status_category). Call once per session to build the ID-to-label mapping. Results are cached for 1 hour.",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": []
            }
        ),
        types.Tool(
            name="find_merge_candidates",
            description="Find standing tickets that new unassigned alerts should be merged into. Runs 2-3 API calls total (batched). Returns each new ticket with scored candidates — match_score is term-overlap fraction. Call get_custom_statuses first to resolve custom_status_id values. Verify before merging.",
            inputSchema={
                "type": "object",
                "properties": {
                    "lookback_days": {
                        "type": "integer",
                        "description": "How far back to search for standing tickets (default 90 days)",
                        "default": 90
                    }
                },
                "required": []
            }
        ),
        types.Tool(
            name="upload_file",
            description="Upload a local file and return an attachment token. Pass token in create_ticket_comment uploads array. Tokens expire in 60 min — upload and comment immediately. Use Foundation's list_downloads to confirm the path.",
            inputSchema={
                "type": "object",
                "properties": {
                    "local_path": {
                        "type": "string",
                        "description": "Absolute path to the file on disk (e.g. C:/Users/JohnMMoore/Downloads/report.docx). Supports ~ expansion."
                    },
                    "filename": {
                        "type": "string",
                        "description": "Optional display name for the attachment in Zendesk. Defaults to the file's basename."
                    }
                },
                "required": ["local_path"]
            }
        )
    ]


@server.call_tool()
async def handle_call_tool(
        name: str,
        arguments: dict[str, Any] | None
) -> list[types.TextContent]:
    """Handle Zendesk tool execution requests"""
    try:
        if name == "get_ticket":
            if not arguments:
                raise ValueError("Missing arguments")
            ticket = zendesk_client.get_ticket(
                arguments["ticket_id"],
                include_comments=arguments.get("include_comments", False),
                comment_limit=min(arguments.get("comment_limit", 5), 20),
            )
            return [types.TextContent(
                type="text",
                text=json.dumps(ticket)
            )]

        elif name == "create_ticket":
            if not arguments:
                raise ValueError("Missing arguments")
            created = zendesk_client.create_ticket(
                subject=arguments.get("subject"),
                description=arguments.get("description"),
                requester_id=arguments.get("requester_id"),
                assignee_id=arguments.get("assignee_id"),
                priority=arguments.get("priority"),
                type=arguments.get("type"),
                tags=arguments.get("tags"),
                custom_fields=arguments.get("custom_fields"),
            )
            return [types.TextContent(
                type="text",
                text=json.dumps({"message": "Ticket created successfully", "ticket": created}, indent=2)
            )]

        elif name == "get_tickets":
            page = arguments.get("page", 1) if arguments else 1
            per_page = arguments.get("per_page", 25) if arguments else 25
            sort_by = arguments.get("sort_by", "created_at") if arguments else "created_at"
            sort_order = arguments.get("sort_order", "desc") if arguments else "desc"

            tickets = zendesk_client.get_tickets(
                page=page,
                per_page=per_page,
                sort_by=sort_by,
                sort_order=sort_order
            )
            return [types.TextContent(
                type="text",
                text=json.dumps(tickets, indent=2)
            )]

        elif name == "get_ticket_comments":
            if not arguments:
                raise ValueError("Missing arguments")
            comments = zendesk_client.get_ticket_comments(
                arguments["ticket_id"])
            return [types.TextContent(
                type="text",
                text=json.dumps(comments)
            )]

        elif name == "create_ticket_comment":
            if not arguments:
                raise ValueError("Missing arguments")
            public = arguments.get("public", True)
            result = zendesk_client.post_comment(
                ticket_id=arguments["ticket_id"],
                comment=arguments["comment"],
                public=public,
                uploads=arguments.get("uploads"),
            )
            return [types.TextContent(
                type="text",
                text=f"Comment created successfully: {result}"
            )]

        elif name == "get_ticket_attachment":
            if not arguments:
                raise ValueError("Missing arguments")
            result = zendesk_client.get_ticket_attachment(arguments["content_url"])
            content_type = result["content_type"]
            if content_type.startswith("image/"):
                return [types.ImageContent(
                    type="image",
                    data=result["data"],
                    mimeType=content_type,
                )]
            else:
                return [types.TextContent(
                    type="text",
                    text=json.dumps({"content_type": content_type, "data_base64": result["data"]})
                )]

        elif name == "update_ticket":
            if not arguments:
                raise ValueError("Missing arguments")
            ticket_id = arguments.get("ticket_id")
            if ticket_id is None:
                raise ValueError("ticket_id is required")
            update_fields = {k: v for k, v in arguments.items() if k != "ticket_id"}
            updated = zendesk_client.update_ticket(ticket_id=int(ticket_id), **update_fields)
            return [types.TextContent(
                type="text",
                text=json.dumps({"message": "Ticket updated successfully", "ticket": updated}, indent=2)
            )]

        elif name == "merge_tickets":
            if not arguments:
                raise ValueError("Missing arguments")
            target_ticket_id = arguments.get("target_ticket_id")
            source_ticket_ids = arguments.get("source_ticket_ids")
            if target_ticket_id is None:
                raise ValueError("target_ticket_id is required")
            if not source_ticket_ids:
                raise ValueError("source_ticket_ids must contain at least one ticket ID")
            if target_ticket_id in source_ticket_ids:
                raise ValueError(
                    f"target_ticket_id ({target_ticket_id}) must not appear in source_ticket_ids"
                )
            result = zendesk_client.merge_tickets(
                target_ticket_id=int(target_ticket_id),
                source_ticket_ids=[int(i) for i in source_ticket_ids],
                target_comment=arguments.get("target_comment"),
                source_comment=arguments.get("source_comment"),
            )
            return [types.TextContent(
                type="text",
                text=json.dumps(
                    {"message": "Merge job submitted successfully", "job_status": result},
                    indent=2
                )
            )]

        elif name == "get_job_status":
            if not arguments:
                raise ValueError("Missing arguments")
            job_id = arguments.get("job_id")
            if not job_id:
                raise ValueError("job_id is required")
            result = zendesk_client.get_job_status(str(job_id))
            return [types.TextContent(
                type="text",
                text=json.dumps(result, indent=2)
            )]

        elif name == "search_tickets":
            if not arguments:
                raise ValueError("Missing arguments")
            query = arguments.get("query", "").strip()
            if not query:
                raise ValueError("query must not be empty")
            results = zendesk_client.search_tickets(
                query=query,
                page=arguments.get("page", 1),
                per_page=arguments.get("per_page", 100),
            )
            return [types.TextContent(
                type="text",
                text=json.dumps(results, indent=2)
            )]

        elif name == "list_organizations":
            results = zendesk_client.list_organizations()
            return [types.TextContent(
                type="text",
                text=json.dumps(results, indent=2)
            )]

        elif name == "find_tech":
            if not arguments:
                raise ValueError("Missing arguments")
            query = arguments.get("query", "").strip()
            if not query:
                raise ValueError("query must not be empty")
            results = _find_tech(query)
            return [types.TextContent(
                type="text",
                text=json.dumps(results, indent=2)
            )]

        elif name == "lookup_user":
            if not arguments:
                raise ValueError("Missing arguments")
            email = arguments.get("email", "").strip()
            if not email:
                raise ValueError("email must not be empty")
            result = lookup_user_cached(email)
            return [types.TextContent(
                type="text",
                text=json.dumps(result, indent=2)
            )]

        elif name == "get_custom_statuses":
            result = get_custom_statuses_cached()
            return [types.TextContent(
                type="text",
                text=json.dumps(result, indent=2)
            )]

        elif name == "find_merge_candidates":
            lookback_days = arguments.get("lookback_days", 90) if arguments else 90
            result = zendesk_client.find_merge_candidates(lookback_days=int(lookback_days))
            return [types.TextContent(
                type="text",
                text=json.dumps(result, indent=2)
            )]

        elif name == "upload_file":
            if not arguments:
                raise ValueError("Missing arguments")
            local_path = arguments.get("local_path", "").strip()
            if not local_path:
                raise ValueError("local_path must not be empty")
            result = zendesk_client.upload_file(
                local_path=local_path,
                filename=arguments.get("filename"),
            )
            return [types.TextContent(
                type="text",
                text=json.dumps(result, indent=2)
            )]

        else:
            raise ValueError(f"Unknown tool: {name}")

    except Exception as e:
        return [types.TextContent(
            type="text",
            text=f"Error: {str(e)}"
        )]


@server.list_resources()
async def handle_list_resources() -> list[types.Resource]:
    logger.debug("Handling list_resources request")
    return [
        types.Resource(
            uri=AnyUrl("zendesk://knowledge-base"),
            name="Zendesk Knowledge Base",
            description="Access to Zendesk Help Center articles and sections",
            mimeType="application/json",
        )
    ]


@ttl_cache(ttl=3600)
def get_cached_kb():
    return zendesk_client.get_all_articles()


@ttl_cache(ttl=3600)
def get_custom_statuses_cached():
    return zendesk_client.get_custom_statuses()


@ttl_cache(ttl=3600)
def lookup_user_cached(email: str):
    return zendesk_client.lookup_user(email)


@server.read_resource()
async def handle_read_resource(uri: AnyUrl) -> str:
    logger.debug(f"Handling read_resource request for URI: {uri}")
    if uri.scheme != "zendesk":
        logger.error(f"Unsupported URI scheme: {uri.scheme}")
        raise ValueError(f"Unsupported URI scheme: {uri.scheme}")

    path = str(uri).replace("zendesk://", "")
    if path != "knowledge-base":
        logger.error(f"Unknown resource path: {path}")
        raise ValueError(f"Unknown resource path: {path}")

    try:
        kb_data = get_cached_kb()
        return json.dumps({
            "knowledge_base": kb_data,
            "metadata": {
                "sections": len(kb_data),
                "total_articles": sum(len(section['articles']) for section in kb_data.values()),
            }
        }, indent=2)
    except Exception as e:
        logger.error(f"Error fetching knowledge base: {e}")
        raise


async def main():
    # Run the server using stdin/stdout streams
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream=read_stream,
            write_stream=write_stream,
            initialization_options=InitializationOptions(
                server_name="Zendesk",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
