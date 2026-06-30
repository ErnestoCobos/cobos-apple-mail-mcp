"""compose_email, reply_to_email, forward_email, create_rich_email_draft.

Plain-text bodies go through JXA directly (the `content` property / a real
`.send()` Apple Event call — not UI automation). HTML bodies are never
typed into Mail's JXA `content` property (unreliable across macOS
versions) and never injected via NSPasteboard/simulated keystrokes
(CLAUDE.md invariant #5); instead a proper multipart MIME message is built
with the stdlib `email` module and opened as a Mail draft via
`open -a Mail <path>.eml`. Because that import only opens a compose
window — Mail's scripting dictionary has no "send this freshly-imported
draft" hook — any HTML-body request here is always a draft/open, never an
auto-send, and that's a deliberate safety property, not a missing feature.
"""

from __future__ import annotations

import sqlite3
import subprocess
import tempfile
from email.message import EmailMessage
from pathlib import Path

from cobos_apple_mail_mcp.config import Config
from cobos_apple_mail_mcp.core.errors import ReadOnlyMode
from cobos_apple_mail_mcp.core.identity import normalize_message_id, to_mail_message_id
from cobos_apple_mail_mcp.core.paths import validate_attachment_path
from cobos_apple_mail_mcp.core.resolver import resolve
from cobos_apple_mail_mcp.write.jxa_executor import JXAExecutor

_EML_OUTBOX = Path.home() / ".cobos-apple-mail-mcp" / "outbox"


def _require_writable(config: Config, operation: str) -> None:
    if config.server.read_only:
        raise ReadOnlyMode(f"server is running --read-only; {operation!r} is disabled")


def _split_addrs(value: str | list[str] | None) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return value
    return [v.strip() for v in value.split(",") if v.strip()]


def _validate_attachments(paths: list[str] | None) -> list[str]:
    if not paths:
        return []
    return [str(validate_attachment_path(p)) for p in paths]


def _build_eml(
    *,
    subject: str,
    to: str | list[str] | None,
    cc: str | list[str] | None,
    bcc: str | list[str] | None,
    text_body: str,
    html_body: str,
    from_address: str | None = None,
) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject or ""
    if from_address:
        msg["From"] = from_address
    if to:
        msg["To"] = ", ".join(_split_addrs(to))
    if cc:
        msg["Cc"] = ", ".join(_split_addrs(cc))
    if bcc:
        msg["Bcc"] = ", ".join(_split_addrs(bcc))
    msg.set_content(text_body or "")
    msg.add_alternative(html_body, subtype="html")
    return msg


def _open_eml_as_draft(msg: EmailMessage) -> Path:
    _EML_OUTBOX.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, raw_path = tempfile.mkstemp(suffix=".eml", dir=str(_EML_OUTBOX))
    path = Path(raw_path)
    with open(fd, "wb") as fh:
        fh.write(bytes(msg))
    path.chmod(0o600)
    subprocess.run(["open", "-a", "Mail", str(path)], check=False, timeout=10)
    return path


def compose_email(
    conn: sqlite3.Connection,
    jxa: JXAExecutor,
    config: Config,
    *,
    account: str,
    to: str | list[str],
    subject: str = "",
    body: str = "",
    cc: str | list[str] | None = None,
    bcc: str | list[str] | None = None,
    attachments: list[str] | None = None,
    mode: str = "send",
    body_html: str | None = None,
    from_address: str | None = None,
) -> dict:
    _require_writable(config, "compose_email")

    if body_html:
        eml = _build_eml(
            subject=subject, to=to, cc=cc, bcc=bcc, text_body=body, html_body=body_html,
            from_address=from_address or account,
        )
        path = _open_eml_as_draft(eml)
        return {
            "status": "draft",
            "note": "HTML body is opened as a draft for review — never auto-sent",
            "eml_path": str(path),
        }

    result = jxa.call(
        "composeEmail",
        {
            "to": _split_addrs(to),
            "cc": _split_addrs(cc),
            "bcc": _split_addrs(bcc),
            "subject": subject,
            "body": body,
            "mode": mode,
            "fromAddress": from_address,
            "attachments": _validate_attachments(attachments),
        },
    )
    return result


def reply_to_email(
    conn: sqlite3.Connection,
    jxa: JXAExecutor,
    config: Config,
    message_id: str,
    *,
    reply_body: str,
    reply_to_all: bool = False,
    cc: str | list[str] | None = None,
    bcc: str | list[str] | None = None,
    attachments: list[str] | None = None,
    mode: str = "send",
    body_html: str | None = None,
    account: str | None = None,
    mailbox: str | None = None,
) -> dict:
    _require_writable(config, "reply_to_email")
    mid = normalize_message_id(message_id)
    resolved = resolve(conn, jxa, mid, account_hint=account, mailbox_hint=mailbox)

    if body_html:
        eml = _build_eml(
            subject="", to=None, cc=cc, bcc=bcc, text_body=reply_body, html_body=body_html
        )
        path = _open_eml_as_draft(eml)
        return {
            "status": "draft",
            "note": (
                "HTML reply body cannot be threaded automatically via the .eml path; "
                "review and send manually, or omit body_html to send via Mail's native reply"
            ),
            "eml_path": str(path),
        }

    result = jxa.call(
        "replyToEmail",
        {
            "accountHint": resolved.account_name,
            "mailboxHint": resolved.mailbox_name,
            "messageId": to_mail_message_id(resolved.canonical_id),
            "replyAll": reply_to_all,
            "body": reply_body,
            "cc": _split_addrs(cc),
            "bcc": _split_addrs(bcc),
            "mode": mode,
            "attachments": _validate_attachments(attachments),
        },
    )
    return result


def forward_email(
    conn: sqlite3.Connection,
    jxa: JXAExecutor,
    config: Config,
    message_id: str,
    *,
    to: str | list[str],
    message: str | None = None,
    cc: str | list[str] | None = None,
    bcc: str | list[str] | None = None,
    mode: str = "send",
    account: str | None = None,
    mailbox: str | None = None,
) -> dict:
    _require_writable(config, "forward_email")
    mid = normalize_message_id(message_id)
    resolved = resolve(conn, jxa, mid, account_hint=account, mailbox_hint=mailbox)

    result = jxa.call(
        "forwardEmail",
        {
            "accountHint": resolved.account_name,
            "mailboxHint": resolved.mailbox_name,
            "messageId": to_mail_message_id(resolved.canonical_id),
            "to": _split_addrs(to),
            "cc": _split_addrs(cc),
            "bcc": _split_addrs(bcc),
            "message": message,
            "mode": mode,
        },
    )
    return result


def create_rich_email_draft(
    conn: sqlite3.Connection,
    jxa: JXAExecutor,
    config: Config,
    *,
    account: str,
    subject: str = "",
    to: str | list[str] | None = None,
    text_body: str = "",
    html_body: str,
    cc: str | list[str] | None = None,
    bcc: str | list[str] | None = None,
    from_address: str | None = None,
) -> dict:
    """Always opens a draft for review — see module docstring for why this
    never auto-sends."""
    _require_writable(config, "create_rich_email_draft")
    eml = _build_eml(
        subject=subject, to=to, cc=cc, bcc=bcc, text_body=text_body, html_body=html_body,
        from_address=from_address or account,
    )
    path = _open_eml_as_draft(eml)
    return {"status": "draft", "eml_path": str(path)}
