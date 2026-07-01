"""MCP resources (`email://...`) — read-only projections of the same
functions backing the tools, so there is one source of truth per CLAUDE.md
knowledge map (Resources and prompts-recipes). Registered into a FastMCP
app by `register_resources(mcp, ctx)`, called from server.py.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from cobos_apple_mail_mcp.knowledge import analytics, contacts, triage
from cobos_apple_mail_mcp.read.threader import get_email_thread
from cobos_apple_mail_mcp.tools import reading

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from cobos_apple_mail_mcp.server import ServerContext


def _dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, list):
        return [_dump(v) for v in value]
    return value


def _dump_json(value: Any) -> str:
    """Serialize to a single JSON string, always — a resource function
    returning a bare Python list (e.g. list[dict]) makes FastMCP try to
    treat the outer list as a list of *resource contents* rather than as
    one content blob containing a list, so list-typed resources must
    return one JSON string, not a raw list.
    """
    return json.dumps(_dump(value), default=str)


def register_resources(mcp: FastMCP, ctx: ServerContext) -> None:
    @mcp.resource("email://accounts")
    def accounts_resource() -> str:
        """All Mail accounts visible in the index."""
        return _dump_json(reading.list_accounts(ctx.conn))

    @mcp.resource("email://mailboxes/{account}")
    def mailboxes_resource(account: str) -> str:
        """Mailboxes for one account."""
        return _dump_json(reading.list_mailboxes(ctx.conn, account=account))

    @mcp.resource("email://threads/{thread_id}")
    def thread_resource(thread_id: str) -> str:
        """One reconstructed conversation thread, by thread_id."""
        return _dump_json(get_email_thread(ctx.conn, thread_id=int(thread_id)))

    @mcp.resource("email://message/{message_id}")
    def message_resource(message_id: str) -> str:
        """One email's full content, by canonical message_id."""
        return _dump_json(reading.get_email(ctx.conn, message_id))

    @mcp.resource("email://contacts")
    def contacts_list_resource() -> str:
        """Browsable contact list, ranked by combined sent+received volume."""
        return _dump_json(contacts.list_contacts(ctx.conn, limit=100))

    @mcp.resource("email://contacts/{address}")
    def contact_resource(address: str) -> str:
        """Derived contact profile: name, message counts, recent threads."""
        return _dump_json(contacts.get_contact(ctx.conn, address))

    @mcp.resource("email://inbox-summary")
    def inbox_summary_resource() -> str:
        """Inbox overview: counts, top senders, newest unread."""
        return _dump_json(analytics.get_inbox_overview(ctx.conn))

    @mcp.resource("email://awaiting-reply")
    def awaiting_reply_resource() -> str:
        """Sent messages with no reply back within the default window."""
        return _dump_json(triage.get_awaiting_reply(ctx.conn))

    @mcp.resource("email://needs-response")
    def needs_response_resource() -> str:
        """Unread inbox messages that look like they need a response."""
        return _dump_json(triage.get_needs_response(ctx.conn))

    @mcp.resource("email://stats")
    def stats_resource() -> str:
        """Account-wide statistics over the default date range."""
        return _dump_json(analytics.get_statistics(ctx.conn))

    @mcp.resource("email://rules")
    def rules_resource() -> str:
        """All Mail rules (read live from Mail via JXA)."""
        from cobos_apple_mail_mcp.write import rules

        return _dump_json(rules.list_rules(ctx.jxa))
