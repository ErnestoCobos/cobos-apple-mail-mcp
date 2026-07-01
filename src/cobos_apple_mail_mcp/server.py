"""FastMCP application: registers tools/resources/prompts and wires the
config-driven dependencies (index connection, mail directory, read-only
enforcement). See CLAUDE.md knowledge map for which module backs each tool.

This module grows across the build phases — Phase 2 wired the read/search
surface; Phase 3 adds threading/knowledge tools and resources; Phase 4 adds
the write layer and the safety guard; Phase 5 adds prompts/recipes.
"""

from __future__ import annotations

import functools
import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from cobos_apple_mail_mcp.cli import _parse_date
from cobos_apple_mail_mcp.config import Config
from cobos_apple_mail_mcp.core.errors import AppleMailMCPError
from cobos_apple_mail_mcp.core.models import SearchMode, SearchScope
from cobos_apple_mail_mcp.core.undo import undo_last as _undo_last
from cobos_apple_mail_mcp.read.indexer import resolve_mail_dir
from cobos_apple_mail_mcp.resources.email_resources import register_resources
from cobos_apple_mail_mcp.skills.loader import register_prompts
from cobos_apple_mail_mcp.storage.database import connect_index
from cobos_apple_mail_mcp.tools import knowledge_tools, reading, search_tools, write_tools
from cobos_apple_mail_mcp.write.jxa_executor import JXAExecutor

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


def _wrap_errors(fn: F) -> F:
    """Convert our typed errors into FastMCP ToolError so the structured
    code/details reach the MCP client instead of being masked as a generic
    failure.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except AppleMailMCPError as exc:
            raise ToolError(f"{exc.code}: {exc.message}") from exc

    return wrapper  # type: ignore[return-value]


def _dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, list):
        return [_dump(v) for v in value]
    return value


class ServerContext:
    """Holds the long-lived resources a server instance needs: the index
    connection, the resolved Mail directory, and config. Built once at
    startup and closed over by every tool/resource function.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self.conn: sqlite3.Connection = connect_index(config.index.path)
        self.mail_dir: Path | None = resolve_mail_dir()
        self.jxa = JXAExecutor(timeout_sec=config.timeouts.jxa_call_sec)

    def default_account(self, account: str | None) -> str | None:
        return account or self.config.defaults.account

    def default_mailbox(self, mailbox: str | None) -> str | None:
        return mailbox or self.config.defaults.mailbox


def build_server(config: Config) -> FastMCP:
    ctx = ServerContext(config)
    mcp: FastMCP = FastMCP(
        name="cobos-apple-mail-mcp",
        instructions=(
            "Unified Apple Mail server: fast on-disk reads/search plus full "
            "AppleScript writes behind a safety layer. Read and knowledge/"
            "triage tools are backed by a local FTS5 index over Apple Mail's "
            "on-disk data; write tools (when not running --read-only) act "
            "through Mail.app via AppleScript/JXA and are resolved by RFC822 "
            "Message-ID, never by guessing among ambiguous matches."
        ),
    )

    # ---------------------------------------------------------------
    # Read tools
    # ---------------------------------------------------------------

    @mcp.tool
    @_wrap_errors
    def list_accounts() -> list[dict]:
        """List configured Mail accounts visible in the index."""
        return _dump(reading.list_accounts(ctx.conn))

    @mcp.tool
    @_wrap_errors
    def list_mailboxes(account: str | None = None) -> list[dict]:
        """List mailboxes (folders) for an account, with unread/total counts."""
        return _dump(reading.list_mailboxes(ctx.conn, account=ctx.default_account(account)))

    @mcp.tool
    @_wrap_errors
    def get_emails(
        account: str | None = None,
        mailbox: str | None = None,
        filter: str = "all",  # noqa: A002
        flag_color: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """List emails. filter: all|unread|flagged|today|last_7_days.
        flag_color filters to one flag color (red|orange|yellow|green|blue|
        purple|gray)."""
        return _dump(
            reading.get_emails(
                ctx.conn,
                account=ctx.default_account(account),
                mailbox=ctx.default_mailbox(mailbox),
                filter=filter,
                flag_color=flag_color,
                limit=limit,
            )
        )

    @mcp.tool
    @_wrap_errors
    def get_email(message_id: str, account: str | None = None, mailbox: str | None = None) -> dict:
        """Fetch one email's full content by its canonical message_id."""
        return _dump(reading.get_email(ctx.conn, message_id, account=account, mailbox=mailbox))

    @mcp.tool
    @_wrap_errors
    def get_email_links(
        message_id: str, account: str | None = None, mailbox: str | None = None
    ) -> list[dict]:
        """Extract hyperlinks from an email's HTML body."""
        return _dump(
            reading.get_email_links(ctx.conn, message_id, account=account, mailbox=mailbox)
        )

    @mcp.tool
    @_wrap_errors
    def get_email_attachment(
        message_id: str,
        filename: str,
        account: str | None = None,
        mailbox: str | None = None,
        save_dir: str | None = None,
    ) -> dict:
        """Extract one attachment from an email and save it to disk."""
        return _dump(
            reading.get_email_attachment(
                ctx.conn, message_id, filename, account=account, mailbox=mailbox, save_dir=save_dir
            )
        )

    @mcp.tool
    @_wrap_errors
    def export_emails(
        output_path: str,
        account: str | None = None,
        mailbox: str | None = None,
        output_format: str = "txt",
        max_emails: int | None = None,
    ) -> dict:
        """Export emails to individual txt/html files under output_path."""
        return reading.export_emails(
            ctx.conn,
            account=account,
            mailbox=mailbox,
            output_format=output_format,
            output_path=output_path,
            max_emails=max_emails,
        )

    # ---------------------------------------------------------------
    # Search & threading
    # ---------------------------------------------------------------

    @mcp.tool
    @_wrap_errors
    def search(
        query: str,
        scope: str = "all",
        mode: str = "keyword",
        account: str | None = None,
        mailbox: str | None = None,
        before: str | None = None,
        after: str | None = None,
        unread_only: bool = False,
        flagged_only: bool = False,
        flag_color: str | None = None,
        has_attachments: bool | None = None,
        limit: int = 25,
        offset: int = 0,
        highlight: bool = True,
    ) -> dict:
        """Full-mailbox search (BM25-ranked). scope: all|subject|sender|body|attachments.
        mode: keyword|semantic|hybrid (semantic/hybrid require the optional
        [semantic] extra; otherwise this degrades to keyword search).
        before/after are YYYY-MM-DD. flag_color filters to one flag color.
        """
        result = search_tools.search(
            ctx.conn,
            query,
            scope=SearchScope(scope),
            mode=SearchMode(mode),
            account=ctx.default_account(account),
            mailbox=mailbox,
            before=_parse_date(before),
            after=_parse_date(after),
            unread_only=unread_only,
            flagged_only=flagged_only,
            flag_color=flag_color,
            has_attachments=has_attachments,
            limit=limit,
            offset=offset,
            highlight=highlight,
            enable_trigram=ctx.config.index.enable_trigram,
            config=ctx.config,
        )
        return _dump(result)

    @mcp.tool
    @_wrap_errors
    def get_email_thread(message_id: str | None = None, thread_id: int | None = None) -> dict:
        """Reconstruct a conversation thread (JWZ threading) by message_id or thread_id."""
        thread = search_tools.get_email_thread(ctx.conn, message_id=message_id, thread_id=thread_id)
        return _dump(thread)

    # ---------------------------------------------------------------
    # Knowledge / triage / analytics
    # ---------------------------------------------------------------

    @mcp.tool
    @_wrap_errors
    def get_inbox_overview(account: str | None = None) -> dict:
        """Inbox counts, top unread senders, needs-response/awaiting-reply totals."""
        resolved_account = ctx.default_account(account)
        overview = knowledge_tools.get_inbox_overview(ctx.conn, account=resolved_account)
        return _dump(overview)

    @mcp.tool
    @_wrap_errors
    def get_awaiting_reply(days_back: int = 7, account: str | None = None) -> list[dict]:
        """Sent messages with no reply back within days_back, ranked by longest-waiting."""
        return _dump(
            knowledge_tools.get_awaiting_reply(
                ctx.conn, days_back=days_back, account=ctx.default_account(account)
            )
        )

    @mcp.tool
    @_wrap_errors
    def get_needs_response(days_back: int = 7, account: str | None = None) -> list[dict]:
        """Unread inbox messages that look like they need a response, ranked by urgency."""
        return _dump(
            knowledge_tools.get_needs_response(
                ctx.conn, days_back=days_back, account=ctx.default_account(account)
            )
        )

    @mcp.tool
    @_wrap_errors
    def get_top_senders(
        account: str | None = None, mailbox: str | None = None, limit: int = 10
    ) -> list[dict]:
        """Top senders by message volume."""
        return _dump(
            knowledge_tools.get_top_senders(
                ctx.conn, account=ctx.default_account(account), mailbox=mailbox, limit=limit
            )
        )

    @mcp.tool
    @_wrap_errors
    def get_statistics(
        scope: str = "account_overview",
        date_range_days: int = 30,
        account: str | None = None,
        sender: str | None = None,
    ) -> dict:
        """Mailbox statistics. scope: account_overview|sender_stats|mailbox_breakdown."""
        return _dump(
            knowledge_tools.get_statistics(
                ctx.conn,
                scope=scope,
                date_range_days=date_range_days,
                account=ctx.default_account(account),
                sender=sender,
            )
        )

    @mcp.tool
    @_wrap_errors
    def list_contacts(
        query: str | None = None, account: str | None = None, limit: int = 25
    ) -> list[dict]:
        """Browse/search contacts, ranked by message volume. Counts mail both
        received from and sent to each address. query substring-matches name
        and address."""
        return _dump(
            knowledge_tools.list_contacts(
                ctx.conn, query=query, account=ctx.default_account(account), limit=limit
            )
        )

    # ---------------------------------------------------------------
    # Write tools — every mutation resolves by canonical message_id
    # (core.resolver) and passes through core.safety.guard(); disabled
    # entirely under --read-only except draft creation (CLAUDE.md
    # invariant #2).
    # ---------------------------------------------------------------

    @mcp.tool
    @_wrap_errors
    def compose_email(
        account: str,
        to: str,
        subject: str,
        body: str,
        cc: str | None = None,
        bcc: str | None = None,
        attachments: list[str] | None = None,
        mode: str = "send",
        body_html: str | None = None,
        from_address: str | None = None,
    ) -> dict:
        """Compose and send (or draft/open) a new email."""
        return write_tools.compose_email(
            ctx.conn,
            ctx.jxa,
            ctx.config,
            account=account,
            to=to,
            subject=subject,
            body=body,
            cc=cc,
            bcc=bcc,
            attachments=attachments,
            mode=mode,
            body_html=body_html,
            from_address=from_address,
        )

    @mcp.tool
    @_wrap_errors
    def reply_to_email(
        message_id: str,
        reply_body: str,
        reply_to_all: bool = False,
        cc: str | None = None,
        bcc: str | None = None,
        attachments: list[str] | None = None,
        mode: str = "send",
        body_html: str | None = None,
        account: str | None = None,
        mailbox: str | None = None,
    ) -> dict:
        """Reply to an email (optionally reply-all), resolved by message_id."""
        return write_tools.reply_to_email(
            ctx.conn,
            ctx.jxa,
            ctx.config,
            message_id,
            reply_body=reply_body,
            reply_to_all=reply_to_all,
            cc=cc,
            bcc=bcc,
            attachments=attachments,
            mode=mode,
            body_html=body_html,
            account=account,
            mailbox=mailbox,
        )

    @mcp.tool
    @_wrap_errors
    def forward_email(
        message_id: str,
        to: str,
        message: str | None = None,
        cc: str | None = None,
        bcc: str | None = None,
        mode: str = "send",
        account: str | None = None,
        mailbox: str | None = None,
    ) -> dict:
        """Forward an email, resolved by message_id."""
        return write_tools.forward_email(
            ctx.conn,
            ctx.jxa,
            ctx.config,
            message_id,
            to=to,
            message=message,
            cc=cc,
            bcc=bcc,
            mode=mode,
            account=account,
            mailbox=mailbox,
        )

    @mcp.tool
    @_wrap_errors
    def create_rich_email_draft(
        account: str,
        html_body: str,
        subject: str = "",
        to: str | None = None,
        text_body: str = "",
        cc: str | None = None,
        bcc: str | None = None,
        from_address: str | None = None,
    ) -> dict:
        """Create a multipart HTML draft. Always opens for review — never auto-sent."""
        return write_tools.create_rich_email_draft(
            ctx.conn,
            ctx.jxa,
            ctx.config,
            account=account,
            subject=subject,
            to=to,
            text_body=text_body,
            html_body=html_body,
            cc=cc,
            bcc=bcc,
            from_address=from_address,
        )

    @mcp.tool
    @_wrap_errors
    def manage_drafts(
        account: str,
        action: str,
        subject: str | None = None,
        to: str | None = None,
        body: str | None = None,
        cc: str | None = None,
        bcc: str | None = None,
        attachments: list[str] | None = None,
        draft_subject: str | None = None,
        from_address: str | None = None,
    ) -> dict:
        """action: list|create|send|open|delete. send/open/delete locate the
        draft by draft_subject within the Drafts mailbox."""
        return write_tools.manage_drafts(
            ctx.conn,
            ctx.jxa,
            ctx.config,
            account=account,
            action=action,
            subject=subject,
            to=to,
            body=body,
            cc=cc,
            bcc=bcc,
            attachments=attachments,
            draft_subject=draft_subject,
            from_address=from_address,
        )

    @mcp.tool
    @_wrap_errors
    def move_email(
        message_ids: list[str],
        to_mailbox: str,
        to_account: str | None = None,
        account: str | None = None,
        mailbox: str | None = None,
        dry_run: bool = False,
        max_moves: int | None = None,
    ) -> dict:
        """Move messages to another mailbox, in the same account or a different one.

        `to_mailbox` is the destination mailbox name; `to_account` is the destination
        account — set it to move ACROSS accounts (without it the destination is looked
        up in the message's own account, and a cross-account destination fails as
        "target mailbox not found"). `account`/`mailbox` are optional hints for the
        SOURCE (where the message currently is) — do not use them for the destination.
        Batch default: 1 (config.batch_limits.move).
        """
        return _dump(
            write_tools.move_email(
                ctx.conn,
                ctx.jxa,
                ctx.config,
                message_ids,
                to_mailbox,
                to_account=to_account,
                account=account,
                mailbox=mailbox,
                dry_run=dry_run,
                max_moves=max_moves,
            )
        )

    @mcp.tool
    @_wrap_errors
    def update_email_status(
        message_ids: list[str],
        action: str,
        color: str | None = None,
        account: str | None = None,
        mailbox: str | None = None,
        dry_run: bool = False,
        max_updates: int | None = None,
    ) -> dict:
        """action: mark_read|mark_unread|flag|unflag|set_flag_color. Batch
        default: 10. set_flag_color needs color (red|orange|yellow|green|blue|
        purple|gray) — Apple Mail's seven colored flags."""
        return _dump(
            write_tools.update_email_status(
                ctx.conn,
                ctx.jxa,
                ctx.config,
                message_ids,
                action,
                color=color,
                account=account,
                mailbox=mailbox,
                dry_run=dry_run,
                max_updates=max_updates,
            )
        )

    @mcp.tool
    @_wrap_errors
    def create_mailbox(account: str, name: str, parent_mailbox: str | None = None) -> dict:
        """Create a mailbox folder ("/" in name for nested hierarchy)."""
        return write_tools.create_mailbox(
            ctx.conn, ctx.jxa, ctx.config, account=account, name=name, parent_mailbox=parent_mailbox
        )

    @mcp.tool
    @_wrap_errors
    def manage_trash(
        action: str,
        account: str,
        message_ids: list[str] | None = None,
        mailbox: str | None = None,
        dry_run: bool = True,
        confirm: bool = False,
        max_deletes: int | None = None,
    ) -> dict:
        """action: move_to_trash|delete_permanent|empty_trash. permanent
        delete/empty-trash require confirm=true; dry_run defaults to true."""
        return _dump(
            write_tools.manage_trash(
                ctx.conn,
                ctx.jxa,
                ctx.config,
                action,
                account=account,
                message_ids=message_ids,
                mailbox=mailbox,
                dry_run=dry_run,
                confirm=confirm,
                max_deletes=max_deletes,
            )
        )

    @mcp.tool
    @_wrap_errors
    def save_email_attachment(
        message_id: str,
        attachment_name: str,
        save_path: str,
        account: str | None = None,
        mailbox: str | None = None,
    ) -> dict:
        """Extract one attachment to an explicit save_path."""
        return _dump(
            write_tools.save_email_attachment(
                ctx.conn, message_id, attachment_name, save_path, account=account, mailbox=mailbox
            )
        )

    @mcp.tool
    @_wrap_errors
    def undo_last(batch_id: str | None = None, dry_run: bool = False) -> dict:
        """Reverse the most recent undoable write batch (move/trash/status/flag only)."""
        return _dump(_undo_last(ctx.conn, ctx.jxa, batch_id=batch_id, dry_run=dry_run))

    @mcp.tool
    @_wrap_errors
    def unsubscribe_from_sender(
        message_id: str,
        account: str | None = None,
        mailbox: str | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Unsubscribe from the list a message belongs to. Prefers an RFC-8058
        one-click https POST (https-only, bounded timeout, no non-https
        redirect); falls back to sending the mailto: unsubscribe. Returns
        method=one-click-post|mailto|none-found so you know what happened.
        Blocked under --read-only (it's an outbound action)."""
        return _dump(
            write_tools.unsubscribe_from_sender(
                ctx.conn,
                ctx.jxa,
                ctx.config,
                message_id,
                account=account,
                mailbox=mailbox,
                dry_run=dry_run,
            )
        )

    @mcp.tool
    @_wrap_errors
    def list_rules() -> list[dict]:
        """List all Mail rules: name, enabled, conditions, and action
        properties. Read-only (rules are read live from Mail via JXA)."""
        return _dump(write_tools.list_rules(ctx.jxa))

    @mcp.tool
    @_wrap_errors
    def enable_rule(name: str, dry_run: bool = False) -> dict:
        """Enable a Mail rule by name. Blocked under --read-only."""
        return _dump(write_tools.set_rule_enabled(ctx.jxa, ctx.config, name, True, dry_run=dry_run))

    @mcp.tool
    @_wrap_errors
    def disable_rule(name: str, dry_run: bool = False) -> dict:
        """Disable a Mail rule by name. Blocked under --read-only."""
        return _dump(
            write_tools.set_rule_enabled(ctx.jxa, ctx.config, name, False, dry_run=dry_run)
        )

    @mcp.tool
    @_wrap_errors
    def delete_rule(name: str, confirm: bool = False, dry_run: bool = False) -> dict:
        """Delete a Mail rule by name. Irreversible — Mail's scripting cannot
        recreate a rule (and cannot create one at all: conditions aren't
        scriptable), so this needs confirm=true and is not undoable. Blocked
        under --read-only."""
        return _dump(
            write_tools.delete_rule(
                ctx.jxa, ctx.config, name, confirm=confirm, dry_run=dry_run
            )
        )

    # ---------------------------------------------------------------
    # Resources (email://...) & recipes (skills/<name> -> MCP prompts)
    # ---------------------------------------------------------------

    register_resources(mcp, ctx)
    register_prompts(mcp)

    return mcp


def run_server(config: Config, *, watch: bool = False) -> None:
    mcp = build_server(config)
    # Start even without Full Disk Access so the client stays connected and the
    # tools can report the problem, instead of the whole server disconnecting
    # with a raw PermissionError traceback. Say clearly what to do.
    from cobos_apple_mail_mcp.read.envelope_reader import library_mail_permission_denied

    if library_mail_permission_denied():
        logger.warning(
            "Cannot read ~/Library/Mail: Full Disk Access is not granted to the app "
            "running this server (e.g. Claude Desktop). Grant it in System Settings -> "
            "Privacy & Security -> Full Disk Access, then fully quit and reopen that app. "
            "Read/search/index tools stay unavailable until then; write tools (Mail.app "
            "scripting) are unaffected."
        )
    if watch:
        from cobos_apple_mail_mcp.read.watcher import start_background_watch

        start_background_watch(config)
    mcp.run()
