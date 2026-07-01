"""Tests for the optional semantic/hybrid search layer. Use a deterministic
fake embedding backend (no real PyObjC/ONNX needed — those are exercised
manually on a real Mac per CLAUDE.md) but a REAL sqlite-vec extension, so
the vec0 KNN integration itself is genuinely tested, not mocked.
"""

from __future__ import annotations

import pytest

from tests.helpers import sqlite_vec_loadable

if not sqlite_vec_loadable():
    # sqlite-vec absent, or this interpreter's sqlite3 can't load extensions
    # (e.g. GitHub's prebuilt macOS Python) — skip rather than fail.
    pytest.skip("sqlite-vec not loadable on this interpreter", allow_module_level=True)

from cobos_apple_mail_mcp.read.indexer import build_index
from cobos_apple_mail_mcp.read.vector_search import (
    embed_backfill,
    hybrid_search,
    semantic_search,
    sync_vec_table,
)
from cobos_apple_mail_mcp.storage.database import connect_index
from tests.helpers import write_message


class FakeEmbeddingBackend:
    """Deterministic bag-of-words-ish embedding: keyword presence -> a
    fixed coordinate. Good enough to validate KNN ordering without any
    real ML dependency."""

    name = "fake"
    dimension = 8
    _VOCAB = ["budget", "lunch", "quarterly", "review", "friday", "tomorrow", "alice", "bob"]

    def is_available(self) -> bool:
        return True

    def embed_one(self, text: str) -> list[float]:
        lowered = text.lower()
        return [1.0 if word in lowered else 0.0 for word in self._VOCAB]

    def embed_many(self, texts):
        return [self.embed_one(t) for t in texts]


def _seed(tmp_path):
    write_message(
        tmp_path,
        rowid=1,
        message_id="m1@x.com",
        subject="Quarterly budget review",
        sender="Alice <alice@example.com>",
        body="Please review the numbers by Friday.",
    )
    write_message(
        tmp_path,
        rowid=2,
        message_id="m2@x.com",
        subject="Lunch plans",
        sender="Bob <bob@example.com>",
        body="Want to grab lunch tomorrow?",
    )
    conn = connect_index(":memory:")
    build_index(conn, tmp_path, full=True)
    return conn


def test_sync_vec_table_creates_table_with_backend_dimension():
    conn = connect_index(":memory:")
    backend = FakeEmbeddingBackend()
    sync_vec_table(conn, backend)
    # Should not raise; table now exists.
    conn.execute("SELECT COUNT(*) FROM emails_vec").fetchone()


def test_embed_backfill_embeds_all_rows_and_marks_state(tmp_path):
    conn = _seed(tmp_path)
    backend = FakeEmbeddingBackend()
    count = embed_backfill(conn, backend)
    assert count == 2
    states = [r["embed_state"] for r in conn.execute("SELECT embed_state FROM emails").fetchall()]
    assert all(s == 2 for s in states)
    vec_count = conn.execute("SELECT COUNT(*) AS n FROM emails_vec").fetchone()["n"]
    assert vec_count == 2


def test_embed_backfill_skips_too_short_text(tmp_path):
    write_message(tmp_path, rowid=1, message_id="m1@x.com", subject="", body="")
    conn = connect_index(":memory:")
    build_index(conn, tmp_path, full=True)
    backend = FakeEmbeddingBackend()
    count = embed_backfill(conn, backend)
    assert count == 0
    state = conn.execute("SELECT embed_state FROM emails").fetchone()["embed_state"]
    assert state == 3


def test_semantic_search_ranks_closest_vector_first(tmp_path):
    conn = _seed(tmp_path)
    backend = FakeEmbeddingBackend()
    embed_backfill(conn, backend)

    result = semantic_search(conn, backend, "budget review", limit=5)
    assert result.returned == 2
    assert result.hits[0].subject == "Quarterly budget review"


def test_hybrid_search_fuses_keyword_and_vector_results(tmp_path):
    conn = _seed(tmp_path)
    backend = FakeEmbeddingBackend()
    embed_backfill(conn, backend)

    result = hybrid_search(conn, backend, "budget")
    assert result.mode.value == "hybrid"
    assert result.returned >= 1
    assert result.hits[0].subject == "Quarterly budget review"


def test_apple_nl_backend_ranks_by_real_semantic_similarity(tmp_path):
    """Uses the REAL Apple NaturalLanguage backend (no fake), skipped if
    PyObjC/NaturalLanguage isn't installed ([semantic] extra). The query
    deliberately shares no words with the target email -- this can only
    pass via genuine semantic similarity, not keyword overlap.
    """
    from cobos_apple_mail_mcp.read.vector_search import AppleNLBackend

    backend = AppleNLBackend()
    if not backend.is_available():
        pytest.skip("Apple NaturalLanguage backend unavailable on this machine")

    write_message(
        tmp_path,
        rowid=1,
        message_id="finance@x.com",
        subject="Quarterly financial review",
        body="Let's go over the numbers for Q3 and discuss the budget allocation.",
    )
    write_message(
        tmp_path,
        rowid=2,
        message_id="lunch@x.com",
        subject="Lunch tomorrow?",
        body="Want to grab pizza at noon?",
    )
    conn = connect_index(":memory:")
    build_index(conn, tmp_path, full=True)
    embed_backfill(conn, backend)

    result = semantic_search(conn, backend, "money and finances discussion", limit=2)
    assert result.hits[0].subject == "Quarterly financial review"


def test_backend_change_invalidates_previous_embeddings(tmp_path):
    conn = _seed(tmp_path)
    backend = FakeEmbeddingBackend()
    embed_backfill(conn, backend)
    assert conn.execute("SELECT COUNT(*) AS n FROM emails_vec").fetchone()["n"] == 2

    class OtherBackend(FakeEmbeddingBackend):
        name = "other"
        dimension = 4

        def embed_one(self, text):
            return [0.0, 0.0, 0.0, 0.0]

    other = OtherBackend()
    sync_vec_table(conn, other)
    # Old 8-dim vectors are gone; embed_state reset so they get re-embedded.
    assert conn.execute("SELECT COUNT(*) AS n FROM emails_vec").fetchone()["n"] == 0
    states = {r["embed_state"] for r in conn.execute("SELECT embed_state FROM emails").fetchall()}
    assert states == {0}
