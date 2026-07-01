---
covers:
  - src/cobos_apple_mail_mcp/server.py
  - src/cobos_apple_mail_mcp/tools/*.py
last_verified: 2026-06-30
---

# Tools reference

All 25 tools are registered in `server.py` and mirrored as CLI subcommands
(`apple-mail-mcp <command> ...`, JSON output). Every parameter name matches between the MCP tool
and its CLI flag (CLI flags are kebab-case, e.g. `to_mailbox` → `--to-mailbox`).

## Read tools (`tools/reading.py`)

| Tool | Parameters | Backend |
|---|---|---|
| `list_accounts` | — | index. `name` is a human display name (e.g. "Work"/"iCloud") resolved from macOS's system `~/Library/Accounts/Accounts4.sqlite` (`read/account_names.py::resolve_account_names()`) at index-build time and stored on `emails.account_name`; falls back to the raw account UUID directory name if that database is missing, unreadable, or has no matching entry — never a hard requirement. `account`/`mailbox` filters on other read tools accept either the resolved name or the UUID. |
| `list_mailboxes` | `account?` | index, with unread/total counts per mailbox |
| `get_emails` | `account?, mailbox?, filter=all\|unread\|flagged\|today\|last_7_days, flag_color?, limit=50` | index. `flag_color` (red\|orange\|yellow\|green\|blue\|purple\|gray) filters to one flag color — see `update_email_status` below for how flag colors are set/stored. |
| `get_email` | `message_id, account?, mailbox?` | index (fast fields) + `.emlx` reparse on demand (HTML body, rich attachments) |
| `get_email_links` | `message_id, account?, mailbox?` | `.emlx` HTML body, filtered (no `mailto:`/`javascript:`/`cid:`/`data:`) |
| `get_email_attachment` | `message_id, filename, account?, mailbox?, save_dir?` | `.emlx` extraction; saved to `save_dir` or `~/.cobos-apple-mail-mcp/attachments/` (mode 0600) |
| `export_emails` | `output_path, account?, mailbox?, output_format=txt\|html, max_emails?` | index; one file per message; `output_path` validated under `$HOME`, sensitive dirs blocked |

## Search & threading (`tools/search_tools.py`)

| Tool | Parameters | Backend |
|---|---|---|
| `search` | `query, scope=all\|subject\|sender\|body\|attachments, mode=keyword\|semantic\|hybrid, account?, mailbox?, before?, after? (YYYY-MM-DD), unread_only=false, flagged_only=false, flag_color?, has_attachments?, limit=25, offset=0, highlight=true` | FTS5 (BM25); trigram fallback if `enable_trigram` and zero hits; semantic/hybrid only if `[semantic]` enabled+available, else degrades to keyword with `degraded: true`. `flag_color` filters to one flag color (plain SQL WHERE, not FTS). |
| `get_email_thread` | `message_id?, thread_id?` (one required) | index, JWZ reconstruction |

## Knowledge / triage / analytics (`tools/knowledge_tools.py`)

| Tool | Parameters | Backend |
|---|---|---|
| `get_inbox_overview` | `account?` | index |
| `get_awaiting_reply` | `days_back=7, account?` | index |
| `get_needs_response` | `days_back=7, account?` | index |
| `get_top_senders` | `account?, mailbox?, limit=10` | index |
| `get_statistics` | `scope=account_overview\|sender_stats\|mailbox_breakdown, date_range_days=30, account?, sender?` | index |
| `list_contacts` | `query?, account?, limit=25` | index. Browsable/searchable contact list, **bidirectional** (counts mail received from *and* sent to each address). Ranked by combined volume, or substring-filtered on name+address when `query` given. Returns `ContactSummary`. See [Threading and knowledge](https://github.com/ErnestoCobos/cobos-apple-mail-mcp/wiki/Threading-and-knowledge#contacts). |

## Write tools (`tools/write_tools.py`) — every batch op gated by `guard()`

| Tool | Parameters | Notes |
|---|---|---|
| `compose_email` | `account, to, subject, body, cc?, bcc?, attachments?, mode=send\|draft\|open, body_html?, from_address?` | `body_html` always opens a draft for review — never auto-sent (see [Architecture](https://github.com/ErnestoCobos/cobos-apple-mail-mcp/wiki/Architecture)) |
| `reply_to_email` | `message_id, reply_body, reply_to_all=false, cc?, bcc?, attachments?, mode=send\|draft\|open, body_html?, account?, mailbox?` | uses Mail's native `reply()` for correct threading headers |
| `forward_email` | `message_id, to, message?, cc?, bcc?, mode=send\|draft\|open, account?, mailbox?` | |
| `create_rich_email_draft` | `account, html_body, subject?, to?, text_body?, cc?, bcc?, from_address?` | always a draft; builds a real MIME `.eml` and opens it via Mail |
| `manage_drafts` | `account, action=list\|create\|send\|open\|delete, subject?, to?, body?, cc?, bcc?, attachments?, draft_subject?, from_address?` | `list`/`create` allowed under `--read-only`; `send` action is unsupported (Mail has no scripted "send this existing draft") — recreate via `compose_email` instead |
| `move_email` | `message_ids[], to_mailbox, account?, mailbox?, dry_run=false, max_moves?` | batch default 1; undoable |
| `update_email_status` | `message_ids[], action=mark_read\|mark_unread\|flag\|unflag\|set_flag_color, color?, account?, mailbox?, dry_run=false, max_updates?` | batch default 10; undoable. `set_flag_color` needs `color` (red\|orange\|yellow\|green\|blue\|purple\|gray) → Mail's `flagIndex` 0-6; sets the flag and optimistically updates the index so it's searchable immediately. See [Apple Mail on-disk format](https://github.com/ErnestoCobos/cobos-apple-mail-mcp/wiki/Apple-Mail-on-disk-format#flag-colors) for why colors set in the Mail UI aren't indexed. |
| `create_mailbox` | `account, name, parent_mailbox?` | `name` may contain `/` for nested hierarchy |
| `manage_trash` | `action=move_to_trash\|delete_permanent\|empty_trash, account, message_ids?, mailbox?, dry_run=true, confirm=false, max_deletes?` | `move_to_trash` undoable; `delete_permanent`/`empty_trash` require `confirm=true`, never undoable, `dry_run` defaults **true** |
| `save_email_attachment` | `message_id, attachment_name, save_path, account?, mailbox?` | `save_path` is an explicit full path (vs. `get_email_attachment`'s default-directory shape) |
| `undo_last` | `batch_id?, dry_run=false` | reverses the most recent undoable batch, or a specific one |

## Resources (`email://...`)

See [Resources and prompts-recipes](https://github.com/ErnestoCobos/cobos-apple-mail-mcp/wiki/Resources-and-prompts-recipes).

## Errors

Every typed error (`core/errors.py`) maps to a stable `code` string surfaced via FastMCP's
`ToolError` (and as `{"error": code, "message": ...}` from the CLI): `not_found`,
`multiple_matches` (carries `candidates`), `handle_superseded`, `read_only_mode`,
`batch_limit_exceeded` (carries `limit`/`requested`), `confirmation_required` (carries
`preview`), `confirmation_stale`, `mail_not_running`, `automation_permission_denied`,
`full_disk_access_denied`, `timeout`, `undo_failed`, `jxa_execution_error`.
