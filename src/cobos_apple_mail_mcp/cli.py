"""Command-line interface: every MCP tool is also a standalone CLI
subcommand with JSON output (CLAUDE.md knowledge map: Configuration
reference / Tools reference). `serve` runs the MCP server itself.
"""

from __future__ import annotations

import argparse
import datetime
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from cobos_apple_mail_mcp.config import (
    DEFAULT_CONFIG_PATH,
    Config,
    generate_default_config_toml,
    load_config,
)
from cobos_apple_mail_mcp.core.errors import AppleMailMCPError
from cobos_apple_mail_mcp.core.models import SearchScope
from cobos_apple_mail_mcp.read.indexer import build_index, get_index_status, resolve_mail_dir
from cobos_apple_mail_mcp.storage.database import connect_index
from cobos_apple_mail_mcp.tools import reading


def _parse_date(value: str | None) -> int | None:
    if not value:
        return None
    dt = datetime.datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
    return int(dt.timestamp())


def _print_json(value: Any) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    elif isinstance(value, list):
        value = [v.model_dump() if hasattr(v, "model_dump") else v for v in value]
    print(json.dumps(value, indent=2, default=str))


def _connect(cfg: Config) -> sqlite3.Connection:
    return connect_index(cfg.index.path)


def _jxa(cfg: Config):
    from cobos_apple_mail_mcp.write.jxa_executor import JXAExecutor

    return JXAExecutor(timeout_sec=cfg.timeouts.jxa_call_sec)


def _mail_dir(cfg: Config) -> Path | None:
    return resolve_mail_dir(None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apple-mail-mcp", description="Unified Apple Mail MCP server"
    )
    parser.add_argument("--config", dest="config_path", default=None, help="path to config.toml")
    parser.add_argument(
        "--read-only", action="store_true", default=False, help="disable all write tools"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="run the MCP server (stdio)")
    p_serve.add_argument("--watch", action="store_true", help="also start the incremental indexer")
    p_serve.add_argument("--verbose", action="store_true")

    p_index = sub.add_parser("index", help="index management")
    index_sub = p_index.add_subparsers(dest="index_command", required=True)
    p_index_build = index_sub.add_parser("build")
    p_index_build.add_argument("--full", action="store_true")
    p_index_build.add_argument("--verbose", action="store_true")
    index_sub.add_parser("rebuild")
    p_index_status = index_sub.add_parser("status")
    p_index_status.add_argument("--verbose", action="store_true")

    sub.add_parser("watch", help="run the incremental indexer in the foreground")

    p_init = sub.add_parser("init", help="generate ~/.cobos-apple-mail-mcp/config.toml")
    p_init.add_argument("--force", action="store_true")

    p_recipe = sub.add_parser("recipe", help="list/run packaged triage recipes")
    recipe_sub = p_recipe.add_subparsers(dest="recipe_command", required=True)
    recipe_sub.add_parser("list")
    p_recipe_run = recipe_sub.add_parser("run")
    p_recipe_run.add_argument("name")
    p_recipe_run.add_argument(
        "--arg",
        dest="args",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="recipe argument, repeatable (e.g. --arg account=Work)",
    )

    p_search = sub.add_parser("search")
    p_search.add_argument("query")
    p_search.add_argument("--scope", choices=[s.value for s in SearchScope], default="all")
    p_search.add_argument("--account")
    p_search.add_argument("--mailbox")
    p_search.add_argument("--limit", type=int, default=20)
    p_search.add_argument("--offset", type=int, default=0)
    p_search.add_argument("--before")
    p_search.add_argument("--after")
    p_search.add_argument("--highlight", action="store_true")
    p_search.add_argument("--mode", choices=["keyword", "semantic", "hybrid"], default="keyword")

    p_read = sub.add_parser("read")
    p_read.add_argument("message_id")
    p_read.add_argument("--account")
    p_read.add_argument("--mailbox")

    p_emails = sub.add_parser("emails")
    p_emails.add_argument("--account")
    p_emails.add_argument("--mailbox")
    p_emails.add_argument(
        "--filter", choices=["all", "unread", "flagged", "today", "last_7_days"], default="all"
    )
    p_emails.add_argument("--limit", type=int, default=50)

    sub.add_parser("accounts")
    p_mailboxes = sub.add_parser("mailboxes")
    p_mailboxes.add_argument("--account")

    p_links = sub.add_parser("links")
    p_links.add_argument("message_id")
    p_links.add_argument("--account")
    p_links.add_argument("--mailbox")

    p_extract = sub.add_parser("extract")
    p_extract.add_argument("message_id")
    p_extract.add_argument("filename")
    p_extract.add_argument("--account")
    p_extract.add_argument("--mailbox")
    p_extract.add_argument("--save-dir")

    p_export = sub.add_parser("export")
    p_export.add_argument("output_path")
    p_export.add_argument("--account")
    p_export.add_argument("--mailbox")
    p_export.add_argument("--format", dest="output_format", choices=["txt", "html"], default="txt")
    p_export.add_argument("--max-emails", type=int)

    p_thread = sub.add_parser("thread")
    p_thread.add_argument("--message-id")
    p_thread.add_argument("--thread-id", type=int)

    p_overview = sub.add_parser("overview")
    p_overview.add_argument("--account")

    p_awaiting = sub.add_parser("awaiting-reply")
    p_awaiting.add_argument("--account")
    p_awaiting.add_argument("--days-back", type=int, default=7)

    p_needs = sub.add_parser("needs-response")
    p_needs.add_argument("--account")
    p_needs.add_argument("--days-back", type=int, default=7)

    p_senders = sub.add_parser("top-senders")
    p_senders.add_argument("--account")
    p_senders.add_argument("--mailbox")
    p_senders.add_argument("--limit", type=int, default=10)

    p_stats = sub.add_parser("statistics")
    p_stats.add_argument(
        "--scope",
        choices=["account_overview", "sender_stats", "mailbox_breakdown"],
        default="account_overview",
    )
    p_stats.add_argument("--date-range-days", type=int, default=30)
    p_stats.add_argument("--account")
    p_stats.add_argument("--sender")

    # --- write commands ---

    p_compose = sub.add_parser("compose")
    p_compose.add_argument("--account", required=True)
    p_compose.add_argument("--to", required=True)
    p_compose.add_argument("--subject", default="")
    p_compose.add_argument("--body", default="")
    p_compose.add_argument("--cc")
    p_compose.add_argument("--bcc")
    p_compose.add_argument("--attachment", dest="attachments", action="append")
    p_compose.add_argument("--mode", choices=["send", "draft", "open"], default="send")
    p_compose.add_argument("--html-body")
    p_compose.add_argument("--from-address")

    p_reply = sub.add_parser("reply")
    p_reply.add_argument("message_id")
    p_reply.add_argument("--body", required=True, dest="reply_body")
    p_reply.add_argument("--reply-all", action="store_true")
    p_reply.add_argument("--cc")
    p_reply.add_argument("--bcc")
    p_reply.add_argument("--mode", choices=["send", "draft", "open"], default="send")
    p_reply.add_argument("--html-body")
    p_reply.add_argument("--account")
    p_reply.add_argument("--mailbox")

    p_forward = sub.add_parser("forward")
    p_forward.add_argument("message_id")
    p_forward.add_argument("--to", required=True)
    p_forward.add_argument("--message")
    p_forward.add_argument("--cc")
    p_forward.add_argument("--bcc")
    p_forward.add_argument("--mode", choices=["send", "draft", "open"], default="send")
    p_forward.add_argument("--account")
    p_forward.add_argument("--mailbox")

    p_rich = sub.add_parser("rich-draft")
    p_rich.add_argument("--account", required=True)
    p_rich.add_argument("--subject", default="")
    p_rich.add_argument("--to")
    p_rich.add_argument("--text-body", default="")
    p_rich.add_argument("--html-body", required=True)
    p_rich.add_argument("--cc")
    p_rich.add_argument("--bcc")

    p_drafts = sub.add_parser("drafts")
    p_drafts.add_argument("--account", required=True)
    p_drafts.add_argument(
        "--action", choices=["list", "create", "send", "open", "delete"], required=True
    )
    p_drafts.add_argument("--subject")
    p_drafts.add_argument("--to")
    p_drafts.add_argument("--body")
    p_drafts.add_argument("--cc")
    p_drafts.add_argument("--bcc")
    p_drafts.add_argument("--draft-subject")

    p_move = sub.add_parser("move")
    p_move.add_argument("message_ids", nargs="+")
    p_move.add_argument("--to-mailbox", required=True)
    p_move.add_argument("--account")
    p_move.add_argument("--mailbox")
    p_move.add_argument("--dry-run", action="store_true")
    p_move.add_argument("--max-moves", type=int)

    p_status = sub.add_parser("status")
    p_status.add_argument("message_ids", nargs="+")
    p_status.add_argument(
        "--action", choices=["mark_read", "mark_unread", "flag", "unflag"], required=True
    )
    p_status.add_argument("--account")
    p_status.add_argument("--mailbox")
    p_status.add_argument("--dry-run", action="store_true")
    p_status.add_argument("--max-updates", type=int)

    p_mkmbox = sub.add_parser("create-mailbox")
    p_mkmbox.add_argument("--account", required=True)
    p_mkmbox.add_argument("--name", required=True)
    p_mkmbox.add_argument("--parent")

    p_trash = sub.add_parser("trash")
    p_trash.add_argument(
        "--action", choices=["move_to_trash", "delete_permanent", "empty_trash"], required=True
    )
    p_trash.add_argument("--account", required=True)
    p_trash.add_argument("message_ids", nargs="*")
    p_trash.add_argument("--mailbox")
    p_trash.add_argument("--dry-run", action="store_true", default=None)
    p_trash.add_argument("--confirm", action="store_true")
    p_trash.add_argument("--max-deletes", type=int)

    p_save_attach = sub.add_parser("save-attachment")
    p_save_attach.add_argument("message_id")
    p_save_attach.add_argument("attachment_name")
    p_save_attach.add_argument("save_path")
    p_save_attach.add_argument("--account")
    p_save_attach.add_argument("--mailbox")

    p_undo = sub.add_parser("undo-last")
    p_undo.add_argument("--batch-id")
    p_undo.add_argument("--dry-run", action="store_true")

    return parser


def _cmd_init(*, force: bool) -> None:
    path = DEFAULT_CONFIG_PATH
    if path.exists() and not force:
        print(f"{path} already exists; use --force to overwrite")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(generate_default_config_toml())
    print(f"wrote {path}")


def _main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "init":
        _cmd_init(force=args.force)
        return 0

    cli_overrides: dict[str, Any] = {}
    if args.read_only:
        cli_overrides["server"] = {"read_only": True}
    cfg = load_config(cli_overrides=cli_overrides, config_path=args.config_path)

    if args.command == "serve":
        from cobos_apple_mail_mcp.server import run_server

        run_server(cfg, watch=args.watch)
        return 0

    if args.command == "recipe":
        from cobos_apple_mail_mcp.skills.loader import discover_recipes, get_recipe, render_recipe

        if args.recipe_command == "list":
            _print_json(
                [
                    {
                        "name": r.name,
                        "description": r.description,
                        "arguments": [
                            {"name": a.name, "required": a.required, "default": a.default}
                            for a in r.arguments
                        ],
                    }
                    for r in discover_recipes()
                ]
            )
            return 0
        if args.recipe_command == "run":
            recipe = get_recipe(args.name)
            if recipe is None:
                raise AppleMailMCPError(f"unknown recipe: {args.name!r}")
            values: dict[str, str] = {}
            for item in args.args:
                if "=" not in item:
                    raise AppleMailMCPError(f"--arg must be KEY=VALUE, got {item!r}")
                key, _, value = item.partition("=")
                values[key] = value
            print(render_recipe(recipe, values))
            return 0
        parser.error(f"unknown recipe subcommand {args.recipe_command!r}")
        return 2

    conn = _connect(cfg)
    mail_dir = _mail_dir(cfg)

    if args.command == "index":
        exclude = set(cfg.index.exclude_mailboxes)
        if args.index_command == "build":
            if mail_dir is None:
                raise AppleMailMCPError("could not locate ~/Library/Mail/V*; is Mail.app set up?")
            result = build_index(
                conn,
                mail_dir,
                exclude_mailboxes=exclude,
                full=args.full,
                enable_trigram=cfg.index.enable_trigram,
            )
            _print_json(result)
        elif args.index_command == "rebuild":
            if mail_dir is None:
                raise AppleMailMCPError("could not locate ~/Library/Mail/V*; is Mail.app set up?")
            result = build_index(
                conn,
                mail_dir,
                exclude_mailboxes=exclude,
                full=True,
                enable_trigram=cfg.index.enable_trigram,
            )
            _print_json(result)
        elif args.index_command == "status":
            status = get_index_status(conn, mail_dir, staleness_hours=cfg.index.staleness_hours)
            _print_json(status)
        return 0

    if args.command == "watch":
        from cobos_apple_mail_mcp.read.watcher import run_watch_loop

        if mail_dir is None:
            raise AppleMailMCPError("could not locate ~/Library/Mail/V*; is Mail.app set up?")
        run_watch_loop(conn, mail_dir, cfg)
        return 0

    if args.command == "search":
        from cobos_apple_mail_mcp.core.models import SearchMode
        from cobos_apple_mail_mcp.tools import search_tools

        result = search_tools.search(
            conn,
            args.query,
            scope=SearchScope(args.scope),
            mode=SearchMode(args.mode),
            account=args.account,
            mailbox=args.mailbox,
            before=_parse_date(args.before),
            after=_parse_date(args.after),
            limit=args.limit,
            offset=args.offset,
            highlight=args.highlight,
            enable_trigram=cfg.index.enable_trigram,
            config=cfg,
        )
        _print_json(result)
        return 0

    if args.command == "read":
        _print_json(
            reading.get_email(conn, args.message_id, account=args.account, mailbox=args.mailbox)
        )
        return 0

    if args.command == "emails":
        _print_json(
            reading.get_emails(
                conn,
                account=args.account,
                mailbox=args.mailbox,
                filter=args.filter,
                limit=args.limit,
            )
        )
        return 0

    if args.command == "accounts":
        _print_json(reading.list_accounts(conn))
        return 0

    if args.command == "mailboxes":
        _print_json(reading.list_mailboxes(conn, account=args.account))
        return 0

    if args.command == "links":
        _print_json(
            reading.get_email_links(
                conn, args.message_id, account=args.account, mailbox=args.mailbox
            )
        )
        return 0

    if args.command == "extract":
        _print_json(
            reading.get_email_attachment(
                conn,
                args.message_id,
                args.filename,
                account=args.account,
                mailbox=args.mailbox,
                save_dir=args.save_dir,
            )
        )
        return 0

    if args.command == "export":
        _print_json(
            reading.export_emails(
                conn,
                account=args.account,
                mailbox=args.mailbox,
                output_format=args.output_format,
                output_path=args.output_path,
                max_emails=args.max_emails,
            )
        )
        return 0

    if args.command == "thread":
        from cobos_apple_mail_mcp.tools import search_tools

        _print_json(
            search_tools.get_email_thread(
                conn, message_id=args.message_id, thread_id=args.thread_id
            )
        )
        return 0

    if args.command == "overview":
        from cobos_apple_mail_mcp.tools import knowledge_tools

        _print_json(knowledge_tools.get_inbox_overview(conn, account=args.account))
        return 0

    if args.command == "awaiting-reply":
        from cobos_apple_mail_mcp.tools import knowledge_tools

        _print_json(
            knowledge_tools.get_awaiting_reply(
                conn, days_back=args.days_back, account=args.account
            )
        )
        return 0

    if args.command == "needs-response":
        from cobos_apple_mail_mcp.tools import knowledge_tools

        _print_json(
            knowledge_tools.get_needs_response(
                conn, days_back=args.days_back, account=args.account
            )
        )
        return 0

    if args.command == "top-senders":
        from cobos_apple_mail_mcp.tools import knowledge_tools

        _print_json(
            knowledge_tools.get_top_senders(
                conn, account=args.account, mailbox=args.mailbox, limit=args.limit
            )
        )
        return 0

    if args.command == "statistics":
        from cobos_apple_mail_mcp.tools import knowledge_tools

        _print_json(
            knowledge_tools.get_statistics(
                conn,
                scope=args.scope,
                date_range_days=args.date_range_days,
                account=args.account,
                sender=args.sender,
            )
        )
        return 0

    if args.command == "compose":
        from cobos_apple_mail_mcp.tools import write_tools

        _print_json(
            write_tools.compose_email(
                conn,
                _jxa(cfg),
                cfg,
                account=args.account,
                to=args.to,
                subject=args.subject,
                body=args.body,
                cc=args.cc,
                bcc=args.bcc,
                attachments=args.attachments,
                mode=args.mode,
                body_html=args.html_body,
                from_address=args.from_address,
            )
        )
        return 0

    if args.command == "reply":
        from cobos_apple_mail_mcp.tools import write_tools

        _print_json(
            write_tools.reply_to_email(
                conn,
                _jxa(cfg),
                cfg,
                args.message_id,
                reply_body=args.reply_body,
                reply_to_all=args.reply_all,
                cc=args.cc,
                bcc=args.bcc,
                mode=args.mode,
                body_html=args.html_body,
                account=args.account,
                mailbox=args.mailbox,
            )
        )
        return 0

    if args.command == "forward":
        from cobos_apple_mail_mcp.tools import write_tools

        _print_json(
            write_tools.forward_email(
                conn,
                _jxa(cfg),
                cfg,
                args.message_id,
                to=args.to,
                message=args.message,
                cc=args.cc,
                bcc=args.bcc,
                mode=args.mode,
                account=args.account,
                mailbox=args.mailbox,
            )
        )
        return 0

    if args.command == "rich-draft":
        from cobos_apple_mail_mcp.tools import write_tools

        _print_json(
            write_tools.create_rich_email_draft(
                conn,
                _jxa(cfg),
                cfg,
                account=args.account,
                subject=args.subject,
                to=args.to,
                text_body=args.text_body,
                html_body=args.html_body,
                cc=args.cc,
                bcc=args.bcc,
            )
        )
        return 0

    if args.command == "drafts":
        from cobos_apple_mail_mcp.tools import write_tools

        _print_json(
            write_tools.manage_drafts(
                conn,
                _jxa(cfg),
                cfg,
                account=args.account,
                action=args.action,
                subject=args.subject,
                to=args.to,
                body=args.body,
                cc=args.cc,
                bcc=args.bcc,
                draft_subject=args.draft_subject,
            )
        )
        return 0

    if args.command == "move":
        from cobos_apple_mail_mcp.tools import write_tools

        _print_json(
            write_tools.move_email(
                conn,
                _jxa(cfg),
                cfg,
                args.message_ids,
                args.to_mailbox,
                account=args.account,
                mailbox=args.mailbox,
                dry_run=args.dry_run,
                max_moves=args.max_moves,
            )
        )
        return 0

    if args.command == "status":
        from cobos_apple_mail_mcp.tools import write_tools

        _print_json(
            write_tools.update_email_status(
                conn,
                _jxa(cfg),
                cfg,
                args.message_ids,
                args.action,
                account=args.account,
                mailbox=args.mailbox,
                dry_run=args.dry_run,
                max_updates=args.max_updates,
            )
        )
        return 0

    if args.command == "create-mailbox":
        from cobos_apple_mail_mcp.tools import write_tools

        _print_json(
            write_tools.create_mailbox(
                conn,
                _jxa(cfg),
                cfg,
                account=args.account,
                name=args.name,
                parent_mailbox=args.parent,
            )
        )
        return 0

    if args.command == "trash":
        from cobos_apple_mail_mcp.tools import write_tools

        dry_run = args.dry_run if args.dry_run is not None else (args.action != "move_to_trash")
        _print_json(
            write_tools.manage_trash(
                conn,
                _jxa(cfg),
                cfg,
                args.action,
                account=args.account,
                message_ids=args.message_ids,
                mailbox=args.mailbox,
                dry_run=dry_run,
                confirm=args.confirm,
                max_deletes=args.max_deletes,
            )
        )
        return 0

    if args.command == "save-attachment":
        from cobos_apple_mail_mcp.tools import write_tools

        _print_json(
            write_tools.save_email_attachment(
                conn,
                args.message_id,
                args.attachment_name,
                args.save_path,
                account=args.account,
                mailbox=args.mailbox,
            )
        )
        return 0

    if args.command == "undo-last":
        from cobos_apple_mail_mcp.tools import write_tools

        _print_json(
            write_tools.undo_last(conn, _jxa(cfg), batch_id=args.batch_id, dry_run=args.dry_run)
        )
        return 0

    parser.error(f"unknown command {args.command!r}")
    return 2


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except AppleMailMCPError as exc:
        print(json.dumps(exc.to_dict(), indent=2), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
