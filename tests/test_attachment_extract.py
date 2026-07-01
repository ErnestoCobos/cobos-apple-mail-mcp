from __future__ import annotations

import pytest

from cobos_apple_mail_mcp.config import load_config
from cobos_apple_mail_mcp.read.attachment_extract import (
    extract_attachment_text,
    extract_backfill,
    extract_text_from_docx,
    extract_text_from_pdf,
)
from cobos_apple_mail_mcp.read.indexer import build_index
from cobos_apple_mail_mcp.read.search import FTS5Backend
from cobos_apple_mail_mcp.storage.database import connect_index
from tests.helpers import make_test_docx, make_test_pdf, write_message_with_attachment

pypdf = pytest.importorskip("pypdf")


def _cfg(**overrides):
    return load_config(cli_overrides=overrides, config_path="/nonexistent", environ={})


# ---- extraction units ----------------------------------------------------


def test_extract_pdf_text():
    pdf = make_test_pdf("Quarterly Invoice Total 4471")
    text = extract_text_from_pdf(pdf)
    assert text is not None
    assert "Invoice" in text and "4471" in text


def test_extract_docx_text():
    docx = make_test_docx("Contract renewal terms and pricing")
    text = extract_text_from_docx(docx)
    assert text == "Contract renewal terms and pricing"


def test_corrupt_files_degrade_to_none():
    assert extract_text_from_pdf(b"%PDF-1.4 broken garbage") is None
    assert extract_text_from_docx(b"not a zip at all") is None


def test_extract_attachment_text_respects_size_cap():
    pdf = make_test_pdf("small")
    assert extract_attachment_text("x.pdf", pdf, max_bytes=len(pdf) - 1) is None  # over cap
    assert extract_attachment_text("x.txt", pdf, max_bytes=10_000) is None  # unsupported type


# ---- backfill + search integration ---------------------------------------


def _seed_pdf(tmp_path, text="Quarterly Invoice Total 4471"):
    write_message_with_attachment(
        tmp_path,
        rowid=1,
        message_id="m1@x.com",
        attachment_name="invoice.pdf",
        attachment_bytes=make_test_pdf(text),
        attachment_mime="application/pdf",
    )
    conn = connect_index(":memory:")
    build_index(conn, tmp_path, full=True)
    return conn


def test_backfill_extracts_and_makes_content_searchable(tmp_path):
    conn = _seed_pdf(tmp_path)
    # The attachment is indexed (name only) but its content isn't searchable yet.
    assert FTS5Backend(conn).search("4471", scope="attachments").returned == 0

    count = extract_backfill(conn, _cfg())
    assert count == 1

    row = conn.execute("SELECT attachment_text, attachment_extract_state FROM emails").fetchone()
    assert "4471" in (row["attachment_text"] or "")
    assert row["attachment_extract_state"] == 2

    # Now scope=attachments matches the extracted PDF content, not just the name.
    result = FTS5Backend(conn).search("4471", scope="attachments")
    assert result.returned == 1
    assert result.hits[0].message_ref.message_id == "m1@x.com"


def test_backfill_marks_skipped_when_nothing_extractable(tmp_path):
    # An attachment type we don't extract (e.g. .png) -> skipped, not done.
    write_message_with_attachment(
        tmp_path,
        rowid=1,
        message_id="m1@x.com",
        attachment_name="photo.png",
        attachment_bytes=b"\x89PNG\r\n\x1a\n fake png bytes",
        attachment_mime="image/png",
    )
    conn = connect_index(":memory:")
    build_index(conn, tmp_path, full=True)
    count = extract_backfill(conn, _cfg())
    assert count == 0
    state = conn.execute("SELECT attachment_extract_state FROM emails").fetchone()[0]
    assert state == 3  # skipped


def test_backfill_is_idempotent(tmp_path):
    conn = _seed_pdf(tmp_path)
    assert extract_backfill(conn, _cfg()) == 1
    # Second run: nothing left in state 0.
    assert extract_backfill(conn, _cfg()) == 0


def test_corrupt_pdf_attachment_does_not_crash_backfill(tmp_path):
    write_message_with_attachment(
        tmp_path,
        rowid=1,
        message_id="m1@x.com",
        attachment_name="broken.pdf",
        attachment_bytes=b"%PDF-1.4 hopelessly corrupt",
        attachment_mime="application/pdf",
    )
    conn = connect_index(":memory:")
    build_index(conn, tmp_path, full=True)
    # Must not raise; the row is marked skipped.
    assert extract_backfill(conn, _cfg()) == 0
    assert conn.execute("SELECT attachment_extract_state FROM emails").fetchone()[0] == 3
