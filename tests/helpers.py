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
    list_unsubscribe_post: str | None = None,
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
    if list_unsubscribe_post:
        headers.append(f"List-Unsubscribe-Post: {list_unsubscribe_post}")
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


def make_test_pdf(text: str) -> bytes:
    """A minimal but valid single-page PDF whose text pypdf can extract."""
    stream = b"BT /F1 24 Tf 72 700 Td (" + text.encode("latin-1") + b") Tj ET"
    objs = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R"
        b"/Resources<</Font<</F1 5 0 R>>>>>>",
        b"<</Length " + str(len(stream)).encode() + b">>stream\n" + stream + b"\nendstream",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += str(i).encode() + b" 0 obj" + obj + b" endobj\n"
    xref_pos = len(out)
    out += b"xref\n0 " + str(len(objs) + 1).encode() + b"\n0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += b"trailer<</Size " + str(len(objs) + 1).encode() + b"/Root 1 0 R>>\nstartxref\n"
    out += str(xref_pos).encode() + b"\n%%EOF\n"
    return out


def make_test_docx(text: str) -> bytes:
    """A minimal valid .docx (zip) whose word/document.xml carries `text`."""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        zf.writestr(
            "word/document.xml",
            '<?xml version="1.0"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>",
        )
    return buf.getvalue()


def write_message_with_attachment(
    mail_dir: Path,
    *,
    account_uuid: str = "AAAAAAAA-1111-2222-3333-444444444444",
    mailbox: str = "INBOX",
    rowid: int,
    message_id: str,
    attachment_name: str,
    attachment_bytes: bytes,
    attachment_mime: str = "application/pdf",
    subject: str = "See attachment",
    flags: int = FLAG_SEEN,
    date_sent: int = 700000000,
) -> Path:
    """Write a real multipart .emlx carrying a binary attachment, so the parser
    reports it via iter_attachments() and extract_attachment_bytes() reads it
    back."""
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = "Alice <alice@example.com>"
    msg["To"] = "Bob <bob@example.com>"
    msg["Subject"] = subject
    msg["Message-ID"] = f"<{message_id}>"
    msg["Date"] = "Mon, 1 Jun 2026 10:00:00 -0700"
    msg.set_content("See the attached file.")
    maintype, subtype = attachment_mime.split("/", 1)
    msg.add_attachment(
        attachment_bytes, maintype=maintype, subtype=subtype, filename=attachment_name
    )
    msg_bytes = msg.as_bytes()
    plist = plistlib.dumps({"flags": flags, "date-sent": date_sent, "date-received": date_sent + 5})
    raw = str(len(msg_bytes)).encode() + b"\n" + msg_bytes + plist

    messages_dir = mail_dir / account_uuid / f"{mailbox}.mbox" / "0" / "0" / "Messages"
    messages_dir.mkdir(parents=True, exist_ok=True)
    path = messages_dir / f"{rowid}.emlx"
    path.write_bytes(raw)
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
