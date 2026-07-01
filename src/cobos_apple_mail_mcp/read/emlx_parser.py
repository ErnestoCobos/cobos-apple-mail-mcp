"""Parse Apple Mail `.emlx` / `.partial.emlx` files: the authoritative source
for our index (CLAUDE.md knowledge map: Apple-Mail-on-disk-format).

Layout: `<byte-count>\\n<RFC822 message><XML plist trailer>`. The byte count
covers exactly the RFC822 message; whatever follows is an Apple property
list with supplementary metadata (flags, dates, remote-id). `.partial.emlx`
holds headers only; the message body's attachments live alongside in a
sibling `Attachments/<rowid>/<n>/<filename>` tree instead of being inlined.
"""

from __future__ import annotations

import plistlib
import re
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from pathlib import Path

from cobos_apple_mail_mcp.core.identity import normalize_message_id
from cobos_apple_mail_mcp.core.text import html_to_text

# messages.flags / plist "flags" bitfield (CLAUDE.md knowledge map: on-disk format).
FLAG_SEEN = 1 << 0
FLAG_ANSWERED = 1 << 1
FLAG_FLAGGED = 1 << 2
FLAG_DELETED = 1 << 3
FLAG_DRAFT = 1 << 4

_ROWID_RE = re.compile(r"^(\d+)(\.partial)?\.emlx$")


@dataclass
class AddressInfo:
    name: str | None
    addr: str


@dataclass
class ParsedEmlx:
    rowid: int
    path: Path
    mtime: float
    size: int
    is_partial: bool

    message_id: str | None = None
    in_reply_to: str | None = None
    references: list[str] = field(default_factory=list)

    subject: str | None = None
    sender: AddressInfo | None = None
    to: list[AddressInfo] = field(default_factory=list)
    cc: list[AddressInfo] = field(default_factory=list)

    date_sent: int | None = None
    date_received: int | None = None

    body_plain: str = ""
    body_html: str | None = None

    is_read: bool = False
    is_answered: bool = False
    is_flagged: bool = False
    is_draft: bool = False
    is_bulk: bool = False

    attachment_names: list[str] = field(default_factory=list)

    @property
    def attachment_count(self) -> int:
        return len(self.attachment_names)

    @property
    def snippet(self) -> str:
        return self.body_plain[:512]


def rowid_from_filename(path: Path) -> int | None:
    match = _ROWID_RE.match(path.name)
    return int(match.group(1)) if match else None


def is_partial_emlx(path: Path) -> bool:
    return path.name.endswith(".partial.emlx")


def _decode_flags(flags: int) -> dict[str, bool]:
    return {
        "is_read": bool(flags & FLAG_SEEN),
        "is_answered": bool(flags & FLAG_ANSWERED),
        "is_flagged": bool(flags & FLAG_FLAGGED),
        "is_draft": bool(flags & FLAG_DRAFT),
    }


def _addresses_from_header(header_value) -> list[AddressInfo]:  # noqa: ANN001
    if header_value is None:
        return []
    addresses = getattr(header_value, "addresses", None)
    if addresses:
        return [AddressInfo(name=a.display_name or None, addr=a.addr_spec) for a in addresses]
    # Fall back to a raw string header (malformed mail policy.default couldn't parse).
    from email.utils import getaddresses

    return [
        AddressInfo(name=name or None, addr=addr)
        for name, addr in getaddresses([str(header_value)])
        if addr
    ]


def _split_references(value: str | None) -> list[str]:
    if not value:
        return []
    return [normalize_message_id(token) for token in re.findall(r"<[^<>]+>", str(value))]


def _looks_bulk(msg) -> bool:  # noqa: ANN001
    """Heuristic newsletter/automated-mail detection, used by the triage
    layer's needs-response/awaiting-reply filters."""
    if msg.get("List-Unsubscribe") or msg.get("List-Id") or msg.get("List-Post"):
        return True
    precedence = str(msg.get("Precedence") or "").strip().lower()
    if precedence in ("bulk", "list", "junk"):
        return True
    auto_submitted = str(msg.get("Auto-Submitted") or "").strip().lower()
    return bool(auto_submitted and auto_submitted != "no")


def _parse_date_header(value) -> int | None:  # noqa: ANN001
    if value is None:
        return None
    try:
        dt = parsedate_to_datetime(str(value))
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    return int(dt.timestamp())


def _sanitize_text(value: str | None) -> str | None:
    """`email.policy.default` decodes malformed/non-UTF-8 headers leniently,
    leaving lone UTF-16 surrogates in the resulting str rather than raising
    (real .emlx files from older or misconfigured senders hit this). Those
    surrogates are valid Python str content but sqlite3 rejects them at
    insert time with UnicodeEncodeError, so every piece of text pulled out
    of a message must be swept here before it reaches the index."""
    if value is None:
        return None
    return value.encode("utf-8", "replace").decode("utf-8")


def _sanitize_parsed(parsed: ParsedEmlx) -> ParsedEmlx:
    parsed.subject = _sanitize_text(parsed.subject)
    parsed.message_id = _sanitize_text(parsed.message_id)
    parsed.in_reply_to = _sanitize_text(parsed.in_reply_to)
    parsed.references = [_sanitize_text(r) or "" for r in parsed.references]
    if parsed.sender is not None:
        parsed.sender = AddressInfo(
            name=_sanitize_text(parsed.sender.name), addr=_sanitize_text(parsed.sender.addr) or ""
        )
    parsed.to = [
        AddressInfo(name=_sanitize_text(a.name), addr=_sanitize_text(a.addr) or "")
        for a in parsed.to
    ]
    parsed.cc = [
        AddressInfo(name=_sanitize_text(a.name), addr=_sanitize_text(a.addr) or "")
        for a in parsed.cc
    ]
    parsed.body_plain = _sanitize_text(parsed.body_plain) or ""
    parsed.body_html = _sanitize_text(parsed.body_html)
    parsed.attachment_names = [_sanitize_text(n) or "" for n in parsed.attachment_names]
    return parsed


def parse_emlx_bytes(raw: bytes, *, rowid: int, path: Path, mtime: float, size: int) -> ParsedEmlx:
    newline_idx = raw.index(b"\n")
    byte_count = int(raw[:newline_idx].strip())
    msg_start = newline_idx + 1
    msg_bytes = raw[msg_start : msg_start + byte_count]
    plist_bytes = raw[msg_start + byte_count :].strip()

    plist: dict = {}
    if plist_bytes:
        try:
            plist = plistlib.loads(plist_bytes)
        except Exception:
            plist = {}

    msg = BytesParser(policy=policy.default).parsebytes(msg_bytes)

    parsed = ParsedEmlx(
        rowid=rowid,
        path=path,
        mtime=mtime,
        size=size,
        is_partial=is_partial_emlx(path),
    )

    message_id = msg.get("Message-ID") or plist.get("message-id")
    if message_id:
        parsed.message_id = normalize_message_id(str(message_id))

    in_reply_to = msg.get("In-Reply-To")
    if in_reply_to:
        refs = _split_references(str(in_reply_to))
        parsed.in_reply_to = refs[0] if refs else None

    parsed.references = _split_references(msg.get("References"))

    parsed.subject = str(msg.get("Subject")) if msg.get("Subject") is not None else None

    from_addrs = _addresses_from_header(msg.get("From"))
    parsed.sender = from_addrs[0] if from_addrs else None
    parsed.to = _addresses_from_header(msg.get("To"))
    parsed.cc = _addresses_from_header(msg.get("Cc"))

    plist_date_sent = plist.get("date-sent")
    plist_date_received = plist.get("date-received")
    parsed.date_sent = (
        int(plist_date_sent) if plist_date_sent is not None else _parse_date_header(msg.get("Date"))
    )
    parsed.date_received = (
        int(plist_date_received) if plist_date_received is not None else parsed.date_sent
    )

    if parsed.is_partial:
        attachments_dir = path.parent.parent / "Attachments" / str(rowid)
        if attachments_dir.is_dir():
            parsed.attachment_names = sorted(
                f.name for f in attachments_dir.rglob("*") if f.is_file()
            )
    else:
        try:
            body_part = msg.get_body(preferencelist=("plain", "html"))
        except Exception:
            body_part = None
        if body_part is not None:
            try:
                content = body_part.get_content()
            except Exception:
                content = ""
            if body_part.get_content_type() == "text/html":
                parsed.body_html = content
                parsed.body_plain = html_to_text(content)
            else:
                parsed.body_plain = content or ""
        try:
            for part in msg.iter_attachments():
                filename = part.get_filename()
                if filename:
                    parsed.attachment_names.append(filename)
        except Exception:
            pass

    parsed.is_bulk = _looks_bulk(msg)

    plist_flags = plist.get("flags")
    if isinstance(plist_flags, int):
        decoded = _decode_flags(plist_flags)
        parsed.is_read = decoded["is_read"]
        parsed.is_answered = decoded["is_answered"]
        parsed.is_flagged = decoded["is_flagged"]
        parsed.is_draft = decoded["is_draft"]

    return _sanitize_parsed(parsed)


def parse_emlx_file(path: Path) -> ParsedEmlx | None:
    rowid = rowid_from_filename(path)
    if rowid is None:
        return None
    stat = path.stat()
    raw = path.read_bytes()
    return parse_emlx_bytes(raw, rowid=rowid, path=path, mtime=stat.st_mtime, size=stat.st_size)


def extract_attachment_bytes(parsed_path: Path, filename: str) -> bytes | None:
    """Read one attachment's raw bytes by filename, for save_email_attachment /
    get_email_attachment. Handles both inline-MIME and .partial.emlx layouts.
    """
    if is_partial_emlx(parsed_path):
        rowid = rowid_from_filename(parsed_path)
        if rowid is None:
            return None
        attachments_dir = parsed_path.parent.parent / "Attachments" / str(rowid)
        for candidate in attachments_dir.rglob(filename):
            if candidate.is_file():
                return candidate.read_bytes()
        return None

    raw = parsed_path.read_bytes()
    newline_idx = raw.index(b"\n")
    byte_count = int(raw[:newline_idx].strip())
    msg_bytes = raw[newline_idx + 1 : newline_idx + 1 + byte_count]
    msg = BytesParser(policy=policy.default).parsebytes(msg_bytes)
    for part in msg.iter_attachments():
        if part.get_filename() == filename:
            payload = part.get_payload(decode=True)
            return payload if isinstance(payload, bytes) else None
    return None


@dataclass
class UnsubscribeInfo:
    """Parsed List-Unsubscribe / List-Unsubscribe-Post (RFC 2369 / RFC 8058)."""

    https_urls: list[str] = field(default_factory=list)
    mailto_to: str | None = None
    mailto_subject: str | None = None
    one_click: bool = False


def _message_from_emlx(path: Path):  # noqa: ANN202
    raw = path.read_bytes()
    newline_idx = raw.index(b"\n")
    byte_count = int(raw[:newline_idx].strip())
    msg_bytes = raw[newline_idx + 1 : newline_idx + 1 + byte_count]
    return BytesParser(policy=policy.default).parsebytes(msg_bytes)


def extract_unsubscribe(path: Path) -> UnsubscribeInfo:
    """Parse the source message's List-Unsubscribe headers on demand (not
    persisted to the index — see get_email_links for the same on-demand
    pattern). Returns every https: URI, the first mailto: target (address +
    optional subject), and whether the sender advertises RFC-8058 one-click
    (`List-Unsubscribe-Post: List-Unsubscribe=One-Click`)."""
    import re as _re
    from email.utils import getaddresses
    from urllib.parse import parse_qs, unquote, urlsplit

    info = UnsubscribeInfo()
    try:
        msg = _message_from_emlx(path)
    except Exception:
        return info

    raw_lu = msg.get("List-Unsubscribe")
    lup = str(msg.get("List-Unsubscribe-Post") or "")
    info.one_click = "one-click" in lup.lower()

    if not raw_lu:
        return info

    for token in _re.findall(r"<([^<>]+)>", str(raw_lu)):
        uri = token.strip()
        lowered = uri.lower()
        if lowered.startswith("https://"):
            info.https_urls.append(uri)
        elif lowered.startswith("mailto:") and info.mailto_to is None:
            parts = urlsplit(uri)
            # parts.path is the address; parts.query may carry subject=...
            addrs = getaddresses([unquote(parts.path)])
            if addrs and addrs[0][1]:
                info.mailto_to = addrs[0][1]
            qs = parse_qs(parts.query)
            subj = qs.get("subject")
            if subj:
                info.mailto_subject = unquote(subj[0])
    return info


_LINK_SCHEME_BLOCKLIST = ("mailto:", "javascript:", "cid:", "data:")


def extract_links(html: str | None) -> list[dict[str, str | None]]:
    """Extract hyperlinks from an HTML body, filtering non-navigable schemes."""
    if not html:
        return []
    try:
        from selectolax.parser import HTMLParser

        tree = HTMLParser(html)
        links: list[dict[str, str | None]] = []
        seen: set[str] = set()
        for node in tree.css("a[href]"):
            url = (node.attributes.get("href") or "").strip()
            if not url or url.lower().startswith(_LINK_SCHEME_BLOCKLIST):
                continue
            if url in seen:
                continue
            seen.add(url)
            links.append({"url": url, "text": node.text(strip=True) or None})
        return links
    except Exception:
        return []
