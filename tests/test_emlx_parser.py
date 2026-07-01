from __future__ import annotations

from pathlib import Path

from cobos_apple_mail_mcp.read.emlx_parser import (
    AddressInfo,
    ParsedEmlx,
    _sanitize_parsed,
    _sanitize_text,
)
from tests.helpers import build_emlx_bytes


def test_sanitize_text_strips_lone_surrogates():
    # email.policy.default's lenient header decoding can leave lone UTF-16
    # surrogates in a str for malformed/non-UTF-8 real-world headers -- this
    # crashed a real index build against a live 209k-message mailbox with
    # UnicodeEncodeError when the value reached sqlite3. Reproduce the exact
    # failure boundary directly rather than the upstream parsing quirk that
    # produced it, since that's what the fix actually guards.
    poisoned = "hello \udcff world"
    assert poisoned.encode("utf-8", "replace")  # sanity: this is genuinely unencodable
    try:
        poisoned.encode("utf-8")
        raise AssertionError("fixture string must be unencodable as-is")
    except UnicodeEncodeError:
        pass

    cleaned = _sanitize_text(poisoned)
    cleaned.encode("utf-8")  # must not raise
    assert cleaned == "hello ? world"


def test_sanitize_text_passes_through_clean_and_none():
    assert _sanitize_text(None) is None
    assert _sanitize_text("clean subject") == "clean subject"


def test_sanitize_parsed_cleans_every_text_field():
    parsed = ParsedEmlx(
        rowid=1,
        path=Path(__file__),
        mtime=0.0,
        size=0,
        is_partial=False,
        message_id="m1@example.com",
        subject="bad \udcff subject",
        sender=AddressInfo(name="Bad \udcfe Name", addr="a@example.com"),
        to=[AddressInfo(name="Also \udcfd Bad", addr="b@example.com")],
        cc=[],
        body_plain="body \udcfc text",
        attachment_names=["attach\udcfb.pdf"],
    )
    cleaned = _sanitize_parsed(parsed)
    for value in (
        cleaned.subject,
        cleaned.sender.name,
        cleaned.sender.addr,
        cleaned.to[0].name,
        cleaned.body_plain,
        cleaned.attachment_names[0],
    ):
        value.encode("utf-8")  # must not raise for any field


def test_parse_emlx_bytes_never_returns_unencodable_text(tmp_path):
    from cobos_apple_mail_mcp.read.emlx_parser import parse_emlx_bytes

    raw = build_emlx_bytes(message_id="clean@example.com", subject="Perfectly normal subject")
    parsed = parse_emlx_bytes(raw, rowid=1, path=tmp_path / "1.emlx", mtime=0.0, size=len(raw))
    parsed.subject.encode("utf-8")
    parsed.body_plain.encode("utf-8")
