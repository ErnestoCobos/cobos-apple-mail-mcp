"""Optional attachment text extraction for content search (config:
[attachments] extract_text, plus the `[attachments]` extra).

PDF text uses `pypdf` (pure-Python; imported lazily/guarded like every other
optional dep). DOCX needs no dependency at all — a .docx is a zip whose text
lives in `word/document.xml` as `<w:t>` runs, parsed here with stdlib
`zipfile` + `ElementTree` (deliberately avoiding python-docx, which requires
the compiled lxml and would break the single-file .pyz).

Extraction is far slower per message than header/body parsing, so it runs as a
low-priority backfill draining `attachment_extract_state=0` a few batches at a
time (like the embedding backfill), never inline in the index build. Any
corrupt/encrypted/oversized file degrades to "skipped" — it must never crash
the build (same discipline as the malformed-header sanitization in
emlx_parser).
"""

from __future__ import annotations

import io
import json
import logging
import sqlite3
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from cobos_apple_mail_mcp.config import Config
from cobos_apple_mail_mcp.read.emlx_parser import extract_attachment_bytes

logger = logging.getLogger(__name__)

# pypdf logs "invalid pdf header"/"EOF marker not found" warnings for the many
# real-world attachments named .pdf that aren't valid PDFs (inline images, HTML,
# etc.). We handle those by degrading to None, so the warnings are just noise —
# quiet them to ERROR.
logging.getLogger("pypdf").setLevel(logging.ERROR)

EXTRACT_BATCH_SIZE = 100
_PDF_EXTS = (".pdf",)
_DOCX_EXTS = (".docx",)

# embed_state-style markers on the attachment_extract_state column.
_STATE_DONE = 2
_STATE_SKIPPED = 3


def _sanitize(text: str) -> str:
    return text.encode("utf-8", "replace").decode("utf-8")


def extract_text_from_pdf(data: bytes) -> str | None:
    try:
        import pypdf  # lazy: absent unless the [attachments] extra is installed
    except ImportError:
        return None
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                reader.decrypt("")  # only trivially-encrypted PDFs; else skip
            except Exception:
                return None
        parts: list[str] = []
        for page in reader.pages:
            try:
                parts.append(page.extract_text() or "")
            except Exception:  # noqa: BLE001 - one bad page shouldn't lose the rest
                continue
        text = "\n".join(parts).strip()
        return _sanitize(text) if text else None
    except Exception:  # noqa: BLE001 - corrupt PDF -> skip, never raise
        logger.debug("PDF extraction failed", exc_info=True)
        return None


def extract_text_from_docx(data: bytes) -> str | None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            xml = zf.read("word/document.xml")
        root = ET.fromstring(xml)
        # Namespace-agnostic: collect every <w:t> run's text (tag ends with '}t').
        parts = [el.text for el in root.iter() if el.tag.endswith("}t") and el.text]
        text = " ".join(parts).strip()
        return _sanitize(text) if text else None
    except Exception:  # noqa: BLE001 - not a valid docx / corrupt -> skip
        logger.debug("DOCX extraction failed", exc_info=True)
        return None


def extract_attachment_text(filename: str, data: bytes, max_bytes: int) -> str | None:
    """Dispatch by file extension. Returns None for unsupported types,
    oversized files, or anything that fails to extract."""
    if len(data) > max_bytes:
        return None
    lower = filename.lower()
    if lower.endswith(_PDF_EXTS):
        return extract_text_from_pdf(data)
    if lower.endswith(_DOCX_EXTS):
        return extract_text_from_docx(data)
    return None


def _extract_for_row(
    emlx_path: str, attachment_names_json: str | None, max_bytes: int
) -> str | None:
    if not emlx_path:
        return None
    path = Path(emlx_path)
    if not path.exists():
        return None
    try:
        names = json.loads(attachment_names_json) if attachment_names_json else []
    except (ValueError, TypeError):
        names = []
    texts: list[str] = []
    for name in names:
        lower = str(name).lower()
        if not (lower.endswith(_PDF_EXTS) or lower.endswith(_DOCX_EXTS)):
            continue
        data = extract_attachment_bytes(path, name)
        if data is None:
            continue
        text = extract_attachment_text(name, data, max_bytes)
        if text:
            texts.append(text)
    combined = " ".join(texts).strip()
    return combined or None


def extract_backfill(
    conn: sqlite3.Connection,
    config: Config,
    *,
    batch_size: int = EXTRACT_BATCH_SIZE,
    max_batches: int | None = None,
) -> int:
    """Drain `attachment_extract_state=0` rows that have attachments, extract
    PDF/DOCX text into `attachment_text`, and mark each done(2)/skipped(3).
    Designed to be called repeatedly at low priority (like embed_backfill).
    The UPDATE that sets attachment_text fires the emails_au trigger, which
    re-indexes the row's FTS `attachments` column to include the text.
    Returns the number of rows that got extractable text this call.
    """
    max_bytes = max(1, config.attachments.max_file_size_mb) * 1024 * 1024
    extracted = 0
    batches = 0
    while max_batches is None or batches < max_batches:
        rows = conn.execute(
            "SELECT id, emlx_path, attachment_names FROM emails "
            "WHERE attachment_extract_state = 0 AND attachment_count > 0 "
            "ORDER BY date_received DESC LIMIT ?",
            (batch_size,),
        ).fetchall()
        if not rows:
            break
        for row in rows:
            text = _extract_for_row(row["emlx_path"], row["attachment_names"], max_bytes)
            if text:
                conn.execute(
                    "UPDATE emails SET attachment_text = ?, attachment_extract_state = ? "
                    "WHERE id = ?",
                    (text, _STATE_DONE, row["id"]),
                )
                extracted += 1
            else:
                conn.execute(
                    "UPDATE emails SET attachment_extract_state = ? WHERE id = ?",
                    (_STATE_SKIPPED, row["id"]),
                )
        conn.commit()
        batches += 1
        if len(rows) < batch_size:
            break
    return extracted
