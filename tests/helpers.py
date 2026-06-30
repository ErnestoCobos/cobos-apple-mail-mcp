"""Shared test fixtures: build synthetic .emlx bytes and a fake on-disk Mail
directory tree, so the suite never needs a real Mac/Mail.app to run.
"""

from __future__ import annotations

import plistlib
from pathlib import Path

from cobos_apple_mail_mcp.read.emlx_parser import FLAG_ANSWERED, FLAG_FLAGGED, FLAG_SEEN


def build_emlx_bytes(
    *,
    message_id: str,
    subject: str = "Test subject",
    sender: str = "Alice Example <alice@example.com>",
    to: str = "Bob Test <bob@example.com>",
    body: str = "Hello, this is a test message.",
    in_reply_to: str | None = None,
    references: list[str] | None = None,
    date: str = "Mon, 1 Jun 2026 10:00:00 -0700",
    flags: int = FLAG_SEEN,
    date_sent: int = 700000000,
    date_received: int | None = None,
    list_unsubscribe: str | None = None,
) -> bytes:
    headers = [
        f"From: {sender}",
        f"To: {to}",
        f"Subject: {subject}",
        f"Message-ID: <{message_id}>",
    ]
    if in_reply_to:
        headers.append(f"In-Reply-To: <{in_reply_to}>")
    if references:
        headers.append("References: " + " ".join(f"<{r}>" for r in references))
    if list_unsubscribe:
        headers.append(f"List-Unsubscribe: {list_unsubscribe}")
    headers.append(f"Date: {date}")
    headers.append("Content-Type: text/plain; charset=utf-8")
    msg = ("\r\n".join(headers) + "\r\n\r\n" + body + "\r\n").encode("utf-8")
    plist = plistlib.dumps(
        {
            "flags": flags,
            "date-sent": date_sent,
            "date-received": date_received if date_received is not None else date_sent + 5,
        }
    )
    return str(len(msg)).encode() + b"\n" + msg + plist


def write_message(
    mail_dir: Path,
    *,
    account_uuid: str = "AAAAAAAA-1111-2222-3333-444444444444",
    mailbox: str = "INBOX",
    rowid: int,
    **kwargs,
) -> Path:
    messages_dir = mail_dir / account_uuid / f"{mailbox}.mbox" / "0" / "0" / "Messages"
    messages_dir.mkdir(parents=True, exist_ok=True)
    path = messages_dir / f"{rowid}.emlx"
    path.write_bytes(build_emlx_bytes(**kwargs))
    return path


class FakeJXAExecutor:
    """Test double for write.jxa_executor.JXAExecutor — the documented mock
    boundary (CLAUDE.md: "Write/JXA paths mocked at the jxa_executor
    boundary so unit tests need no live Mail"). Programmed per function
    name with a handler `dict -> Any`; records every call for assertions.
    """

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self._handlers: dict = {}

    def on(self, function_name: str, handler) -> None:
        self._handlers[function_name] = handler

    def call(self, function_name: str, args: dict):
        self.calls.append((function_name, dict(args)))
        handler = self._handlers.get(function_name)
        if handler is None:
            raise AssertionError(f"FakeJXAExecutor: no handler registered for {function_name!r}")
        return handler(args)

    def ensure_running(self, *, timeout_sec: float = 15.0) -> bool:
        return True


__all__ = [
    "build_emlx_bytes",
    "write_message",
    "FakeJXAExecutor",
    "FLAG_SEEN",
    "FLAG_ANSWERED",
    "FLAG_FLAGGED",
]
