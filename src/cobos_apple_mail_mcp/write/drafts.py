"""manage_drafts: list, create, send, open, delete.

`list` and `create` stay allowed under --read-only (CLAUDE.md invariant #2:
"draft creation stays allowed" — creating a draft mutates nothing the user
already has). `send`/`open`/`delete` act on an existing draft and are
gated like any other write. Unsent drafts have no reliable RFC822
Message-ID, so existing-draft actions locate by subject scoped to the
Drafts mailbox only (see write/scripts/mail_core.js::manageDrafts) — a
deliberately narrower, lower-risk version of the subject matching this
project otherwise replaces with id-based resolution everywhere else.
"""

from __future__ import annotations

import sqlite3

from cobos_apple_mail_mcp.config import Config
from cobos_apple_mail_mcp.core.errors import ReadOnlyMode
from cobos_apple_mail_mcp.core.paths import validate_attachment_path
from cobos_apple_mail_mcp.write.jxa_executor import JXAExecutor

_ACTIONS = ("list", "create", "send", "open", "delete")


def _split(value: str | list[str] | None) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return value
    return [v.strip() for v in value.split(",") if v.strip()]


def manage_drafts(
    conn: sqlite3.Connection,
    jxa: JXAExecutor,
    config: Config,
    *,
    account: str,
    action: str,
    subject: str | None = None,
    to: str | list[str] | None = None,
    body: str | None = None,
    cc: str | list[str] | None = None,
    bcc: str | list[str] | None = None,
    attachments: list[str] | None = None,
    draft_subject: str | None = None,
    from_address: str | None = None,
) -> dict:
    if action not in _ACTIONS:
        raise ValueError(f"unknown drafts action: {action!r}; expected one of {_ACTIONS}")

    if action not in ("list", "create") and config.server.read_only:
        raise ReadOnlyMode(
            f"server is running --read-only; manage_drafts(action={action!r}) is disabled"
        )

    args: dict = {"account": account, "action": action}

    if action == "create":
        args.update(
            {
                "subject": subject or "",
                "to": _split(to),
                "body": body or "",
                "cc": _split(cc),
                "bcc": _split(bcc),
                "attachments": [str(validate_attachment_path(p)) for p in (attachments or [])],
            }
        )
    elif action in ("send", "open", "delete"):
        if not draft_subject:
            raise ValueError(
                f"manage_drafts(action={action!r}) requires draft_subject to locate the draft"
            )
        args["draftSubject"] = draft_subject

    return jxa.call("manageDrafts", args)
