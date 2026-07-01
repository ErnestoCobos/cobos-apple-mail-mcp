"""Read tools: list_accounts, list_mailboxes, get_emails, get_email,
get_email_links, get_email_attachment, export_emails (CLAUDE.md knowledge
map: Tools reference). All served from the fast index; `get_email` /
`get_email_links` / `get_email_attachment` re-parse the source `.emlx` on
demand for rich fields (HTML body, attachment bytes) not duplicated in the
index. `list_accounts`/`list_mailboxes` take an optional JXA-sourced
override for human-friendly account names — wired in by server.py once the
write layer (and its JXA executor) exists; until then they degrade to the
account UUIDs visible on disk, which is enough to operate.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from cobos_apple_mail_mcp.core.errors import NotFound
from cobos_apple_mail_mcp.core.models import (
    Account,
    Attachment,
    EmailFull,
    EmailLink,
    EmailSummary,
    Mailbox,
)
from cobos_apple_mail_mcp.core.paths import validate_output_path
from cobos_apple_mail_mcp.read.emlx_parser import (
    ParsedEmlx,
    extract_attachment_bytes,
    extract_links,
    parse_emlx_file,
)
from cobos_apple_mail_mcp.read.rowmap import row_to_full, row_to_summary

DEFAULT_ATTACHMENTS_DIR = Path.home() / ".cobos-apple-mail-mcp" / "attachments"

_FILTERS = ("all", "unread", "flagged", "today", "last_7_days")


def _start_of_today_utc() -> int:
    import datetime

    now = datetime.datetime.now(datetime.timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp())


def _find_row(
    conn: sqlite3.Connection,
    message_id: str,
    *,
    account: str | None = None,
    mailbox: str | None = None,
) -> sqlite3.Row | None:
    where = ["message_id = :mid"]
    params: dict[str, object] = {"mid": message_id}
    if account:
        where.append("(account_uuid = :account OR account_name = :account)")
        params["account"] = account
    if mailbox:
        where.append("(mailbox_name = :mailbox OR mailbox_role = :mailbox)")
        params["mailbox"] = mailbox
    # If the id is ambiguous (same Message-ID in multiple mailboxes — a
    # known, expected case), reads pick the most recent copy rather than
    # failing; writes use core/resolver.py's stricter MultipleMatches rule.
    rows = conn.execute(
        f"SELECT * FROM emails WHERE {' AND '.join(where)} ORDER BY date_received DESC", params
    ).fetchall()
    return rows[0] if rows else None


def list_accounts(
    conn: sqlite3.Connection, *, jxa_accounts: list[Account] | None = None
) -> list[Account]:
    if jxa_accounts is not None:
        return jxa_accounts
    rows = conn.execute(
        "SELECT DISTINCT account_uuid, account_name FROM emails ORDER BY account_uuid"
    ).fetchall()
    return [
        Account(name=r["account_name"] or r["account_uuid"], uuid=r["account_uuid"]) for r in rows
    ]


def list_mailboxes(
    conn: sqlite3.Connection,
    *,
    account: str | None = None,
    jxa_mailboxes: list[Mailbox] | None = None,
) -> list[Mailbox]:
    if jxa_mailboxes is not None:
        return jxa_mailboxes
    where = ["1=1"]
    params: dict[str, object] = {}
    if account:
        where.append("(account_uuid = :account OR account_name = :account)")
        params["account"] = account
    rows = conn.execute(
        f"""
        SELECT account_uuid, account_name, mailbox_name, mailbox_role,
               COUNT(*) AS total, SUM(CASE WHEN flag_read = 0 THEN 1 ELSE 0 END) AS unread
        FROM emails WHERE {' AND '.join(where)}
        GROUP BY account_uuid, mailbox_name
        ORDER BY account_uuid, mailbox_name
        """,
        params,
    ).fetchall()
    return [
        Mailbox(
            name=r["mailbox_name"],
            account=r["account_name"] or r["account_uuid"],
            role=r["mailbox_role"],
            unread_count=r["unread"] or 0,
            total_count=r["total"],
        )
        for r in rows
    ]


def get_emails(
    conn: sqlite3.Connection,
    *,
    account: str | None = None,
    mailbox: str | None = None,
    filter: str = "all",  # noqa: A002 - matches the spec's tool parameter name
    flag_color: str | None = None,
    limit: int = 50,
) -> list[EmailSummary]:
    if filter not in _FILTERS:
        raise ValueError(f"unknown filter {filter!r}; expected one of {_FILTERS}")
    limit = max(1, min(limit, 200))

    where = ["1=1"]
    params: dict[str, object] = {"limit": limit}
    if account:
        where.append("(account_uuid = :account OR account_name = :account)")
        params["account"] = account
    if mailbox:
        where.append("(mailbox_name = :mailbox OR mailbox_role = :mailbox)")
        params["mailbox"] = mailbox
    if flag_color:
        from cobos_apple_mail_mcp.core.flags import color_to_index

        where.append("flag_color = :flag_color")
        params["flag_color"] = color_to_index(flag_color)
    if filter == "unread":
        where.append("flag_read = 0")
    elif filter == "flagged":
        where.append("flag_flagged = 1")
    elif filter == "today":
        where.append("date_received >= :since")
        params["since"] = _start_of_today_utc()
    elif filter == "last_7_days":
        where.append("date_received >= :since")
        params["since"] = int(time.time()) - 7 * 86400

    where_sql = " AND ".join(where)
    rows = conn.execute(
        f"SELECT * FROM emails WHERE {where_sql} ORDER BY date_received DESC LIMIT :limit",
        params,
    ).fetchall()
    return [row_to_summary(r) for r in rows]


def get_email(
    conn: sqlite3.Connection,
    message_id: str,
    *,
    account: str | None = None,
    mailbox: str | None = None,
) -> EmailFull:
    row = _find_row(conn, message_id, account=account, mailbox=mailbox)
    if row is None:
        raise NotFound(f"no email found for message_id={message_id!r}")

    parsed: ParsedEmlx | None = None
    path = Path(row["emlx_path"]) if row["emlx_path"] else None
    if path is not None and path.exists():
        try:
            parsed = parse_emlx_file(path)
        except Exception:  # noqa: BLE001 - fall back to the indexed plain body
            parsed = None
    return row_to_full(row, parsed)


def get_email_links(
    conn: sqlite3.Connection,
    message_id: str,
    *,
    account: str | None = None,
    mailbox: str | None = None,
) -> list[EmailLink]:
    row = _find_row(conn, message_id, account=account, mailbox=mailbox)
    if row is None:
        raise NotFound(f"no email found for message_id={message_id!r}")
    path = Path(row["emlx_path"]) if row["emlx_path"] else None
    if path is None or not path.exists():
        return []
    parsed = parse_emlx_file(path)
    html = parsed.body_html if parsed else None
    return [EmailLink(url=link["url"], text=link["text"]) for link in extract_links(html)]


def get_email_attachment(
    conn: sqlite3.Connection,
    message_id: str,
    filename: str,
    *,
    account: str | None = None,
    mailbox: str | None = None,
    save_dir: str | None = None,
) -> Attachment:
    row = _find_row(conn, message_id, account=account, mailbox=mailbox)
    if row is None:
        raise NotFound(f"no email found for message_id={message_id!r}")
    path = Path(row["emlx_path"]) if row["emlx_path"] else None
    if path is None or not path.exists():
        raise NotFound(f"source .emlx is no longer on disk for message_id={message_id!r}")

    data = extract_attachment_bytes(path, filename)
    if data is None:
        raise NotFound(f"attachment {filename!r} not found on message {message_id!r}")

    target_dir = validate_output_path(save_dir) if save_dir else DEFAULT_ATTACHMENTS_DIR
    target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    target_path = target_dir / filename
    target_path.write_bytes(data)
    target_path.chmod(0o600)

    return Attachment(filename=filename, size=len(data), saved_path=str(target_path))


def export_emails(
    conn: sqlite3.Connection,
    *,
    account: str | None = None,
    mailbox: str | None = None,
    output_format: str = "txt",
    output_path: str,
    max_emails: int | None = None,
) -> dict[str, object]:
    if output_format not in ("txt", "html"):
        raise ValueError("output_format must be 'txt' or 'html'")

    target_dir = validate_output_path(output_path)
    target_dir.mkdir(parents=True, exist_ok=True)

    where = ["1=1"]
    params: dict[str, object] = {}
    if account:
        where.append("(account_uuid = :account OR account_name = :account)")
        params["account"] = account
    if mailbox:
        where.append("(mailbox_name = :mailbox OR mailbox_role = :mailbox)")
        params["mailbox"] = mailbox
    sql = f"SELECT * FROM emails WHERE {' AND '.join(where)} ORDER BY date_received DESC"
    if max_emails:
        sql += " LIMIT :limit"
        params["limit"] = max_emails

    rows = conn.execute(sql, params).fetchall()
    count = 0
    for row in rows:
        ext = "html" if output_format == "html" else "txt"
        raw_subject = row["subject"] or "no-subject"
        safe_subject = "".join(c for c in raw_subject if c.isalnum() or c in " -_")[:60]
        filename = f"{row['id']}_{safe_subject or 'no-subject'}.{ext}"
        body = row["body_plain"] or ""
        if output_format == "html":
            content = f"<html><body><h1>{row['subject'] or ''}</h1><pre>{body}</pre></body></html>"
        else:
            content = (
                f"From: {row['sender_name'] or ''} <{row['sender_addr'] or ''}>\n"
                f"Subject: {row['subject'] or ''}\n\n{body}"
            )
        (target_dir / filename).write_text(content, encoding="utf-8")
        count += 1

    return {"output_path": str(target_dir), "exported_count": count, "format": output_format}
