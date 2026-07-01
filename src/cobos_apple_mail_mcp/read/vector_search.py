"""Optional semantic/hybrid search layer (CLAUDE.md knowledge map: Search).
OFF by default; requires the `[semantic]` extra. The default backend is
Apple's NaturalLanguage `NLEmbedding` via PyObjC — built into macOS, no
model download — chosen specifically for being the most resource-frugal
option available (the user's explicit preference). `MiniLMBackend` (ONNX)
is an opt-in fallback for multilingual text or when NaturalLanguage is
unavailable. Both backends import their native dependencies lazily so the
core package has zero heavy dependencies when this layer is disabled
(CLAUDE.md packaging notes).

Hybrid search fuses BM25 (read/search.py::FTS5Backend) and vector KNN
results with Reciprocal Rank Fusion (k=60) — no cross-scale score
comparison needed, just rank position in each list.
"""

from __future__ import annotations

import logging
import sqlite3
import struct
import time
from abc import ABC, abstractmethod

from cobos_apple_mail_mcp.config import Config
from cobos_apple_mail_mcp.core.models import SearchHit, SearchMode, SearchResult, SearchScope
from cobos_apple_mail_mcp.read.rowmap import row_to_summary
from cobos_apple_mail_mcp.read.search import FTS5Backend, summary_to_hit
from cobos_apple_mail_mcp.storage.database import (
    get_sync_state,
    set_sync_state,
    try_load_sqlite_vec,
)
from cobos_apple_mail_mcp.storage.migrations import ensure_vec_table

logger = logging.getLogger(__name__)

RRF_K = 60
CANDIDATE_N = 100
EMBED_BATCH_SIZE = 64
MIN_TEXT_CHARS = 8


class EmbeddingBackend(ABC):
    name: str
    dimension: int

    @abstractmethod
    def is_available(self) -> bool: ...

    @abstractmethod
    def embed_one(self, text: str) -> list[float]: ...

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_one(t) for t in texts]


class AppleNLBackend(EmbeddingBackend):
    """Apple NaturalLanguage `NLEmbedding.sentenceEmbeddingForLanguage:`,
    reached via PyObjC. ~512-dim, on-device, no model file. This is the
    riskiest, most macOS/PyObjC-version-sensitive code in the project —
    every call is defensively wrapped so a binding mismatch degrades to
    "unavailable" rather than crashing the server. Verify on a real Mac
    before relying on it (see Wiki: Development & contributing).
    """

    name = "apple_nl"
    dimension = 512

    def __init__(self, language: str = "en") -> None:
        self._language = language
        self._embedding = None
        self._checked = False
        self._ok = False

    def _ensure(self) -> bool:
        if self._checked:
            return self._ok
        self._checked = True
        try:
            from NaturalLanguage import NLEmbedding

            embedding = NLEmbedding.sentenceEmbeddingForLanguage_(self._language)
            if embedding is not None:
                self._embedding = embedding
                self._ok = True
        except Exception:  # noqa: BLE001 - any binding/runtime issue -> unavailable, not a crash
            logger.debug("Apple NLEmbedding unavailable", exc_info=True)
            self._ok = False
        return self._ok

    def is_available(self) -> bool:
        return self._ensure()

    def embed_one(self, text: str) -> list[float]:
        if not self._ensure():
            raise RuntimeError("Apple NaturalLanguage embedding is unavailable on this system")
        vector = self._embedding.vectorForString_(text)
        if vector is None:
            return [0.0] * self.dimension
        return [float(v) for v in vector]


class MiniLMBackend(EmbeddingBackend):
    """ONNX MiniLM-style sentence embeddings. Opt-in fallback: requires the
    `[semantic-minilm]` extra (onnxruntime, tokenizers, numpy) AND a local
    model directory (model.onnx + tokenizer.json) — no auto-download, to
    keep this layer's behavior local-only and predictable.
    """

    name = "minilm"
    dimension = 384

    def __init__(self, model_dir: str | None) -> None:
        self._model_dir = model_dir
        self._session = None
        self._tokenizer = None
        self._checked = False
        self._ok = False

    def _ensure(self) -> bool:
        if self._checked:
            return self._ok
        self._checked = True
        if not self._model_dir:
            return False
        try:
            from pathlib import Path

            import onnxruntime as ort
            from tokenizers import Tokenizer

            model_path = Path(self._model_dir).expanduser() / "model.onnx"
            tokenizer_path = Path(self._model_dir).expanduser() / "tokenizer.json"
            if not model_path.is_file() or not tokenizer_path.is_file():
                logger.warning("MiniLM model files not found under %s", self._model_dir)
                return False
            self._session = ort.InferenceSession(
                str(model_path), providers=["CPUExecutionProvider"]
            )
            self._tokenizer = Tokenizer.from_file(str(tokenizer_path))
            self._ok = True
        except Exception:  # noqa: BLE001
            logger.debug("MiniLM backend unavailable", exc_info=True)
            self._ok = False
        return self._ok

    def is_available(self) -> bool:
        return self._ensure()

    def embed_one(self, text: str) -> list[float]:
        return self.embed_many([text])[0]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        if not self._ensure():
            raise RuntimeError("MiniLM backend is unavailable (missing model files or onnxruntime)")
        import numpy as np

        encodings = [self._tokenizer.encode(t) for t in texts]
        max_len = max(len(e.ids) for e in encodings)
        input_ids = np.zeros((len(texts), max_len), dtype=np.int64)
        attention_mask = np.zeros((len(texts), max_len), dtype=np.int64)
        for i, enc in enumerate(encodings):
            input_ids[i, : len(enc.ids)] = enc.ids
            attention_mask[i, : len(enc.ids)] = 1

        outputs = self._session.run(
            None, {"input_ids": input_ids, "attention_mask": attention_mask}
        )
        token_embeddings = outputs[0]
        mask = attention_mask[..., None].astype(np.float32)
        summed = (token_embeddings * mask).sum(axis=1)
        counts = mask.sum(axis=1).clip(min=1e-9)
        mean_pooled = summed / counts
        norms = np.linalg.norm(mean_pooled, axis=1, keepdims=True).clip(min=1e-9)
        return (mean_pooled / norms).tolist()


def get_backend(config: Config) -> EmbeddingBackend | None:
    """Resolve the configured embedding backend; returns None (never
    raises) when embeddings are disabled or the backend isn't actually
    available, so callers can degrade to keyword search.
    """
    if not config.embeddings.enabled:
        return None
    if config.embeddings.backend == "apple_nl":
        backend: EmbeddingBackend = AppleNLBackend()
    elif config.embeddings.backend == "minilm":
        backend = MiniLMBackend(config.embeddings.model)
    else:
        return None
    return backend if backend.is_available() else None


def _pack_vector(vector: list[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


def sync_vec_table(conn: sqlite3.Connection, backend: EmbeddingBackend) -> None:
    """Ensure emails_vec exists with the right dimension for `backend`. A
    backend change (different model/dimension) invalidates previously
    embedded rows rather than mixing vectors from two different spaces.
    """
    if not try_load_sqlite_vec(conn):
        raise RuntimeError(
            "sqlite-vec extension is not installed; pip install 'cobos-apple-mail-mcp[semantic]'"
        )
    recorded = get_sync_state(conn, "embed_backend")
    current = f"{backend.name}:{backend.dimension}"
    if recorded and recorded != current:
        conn.execute("DROP TABLE IF EXISTS emails_vec")
        conn.execute("UPDATE emails SET embed_state = 0 WHERE embed_state = 2")
        conn.commit()
    ensure_vec_table(conn, backend.dimension)
    set_sync_state(conn, "embed_backend", current)


def embed_backfill(
    conn: sqlite3.Connection,
    backend: EmbeddingBackend,
    *,
    batch_size: int = EMBED_BATCH_SIZE,
    max_batches: int | None = None,
) -> int:
    """Drain embed_state=0 rows into emails_vec, a few batches at a time —
    designed to be called repeatedly (e.g. from the watch loop) at low
    priority rather than run to completion in one call. Returns the number
    of rows embedded this call.
    """
    sync_vec_table(conn, backend)
    total = 0
    batches = 0
    while max_batches is None or batches < max_batches:
        rows = conn.execute(
            "SELECT id, subject, snippet FROM emails WHERE embed_state = 0 "
            "ORDER BY date_received DESC LIMIT ?",
            (batch_size,),
        ).fetchall()
        if not rows:
            break

        texts, ids, too_short = [], [], []
        for row in rows:
            text = f"{row['subject'] or ''}\n{row['snippet'] or ''}".strip()
            if len(text) < MIN_TEXT_CHARS:
                too_short.append(row["id"])
            else:
                texts.append(text)
                ids.append(row["id"])

        if too_short:
            conn.executemany(
                "UPDATE emails SET embed_state = 3 WHERE id = ?", [(i,) for i in too_short]
            )
        if texts:
            vectors = backend.embed_many(texts)
            for email_id, vector in zip(ids, vectors, strict=True):
                conn.execute(
                    "INSERT OR REPLACE INTO emails_vec(email_id, embedding) VALUES (?, ?)",
                    (email_id, _pack_vector(vector)),
                )
            conn.executemany(
                "UPDATE emails SET embed_state = 2 WHERE id = ?", [(i,) for i in ids]
            )
            total += len(texts)

        conn.commit()
        batches += 1
        if len(rows) < batch_size:
            break
    return total


def _vector_candidates(
    conn: sqlite3.Connection, backend: EmbeddingBackend, query: str, *, limit: int
) -> list[tuple[int, float]]:
    if not try_load_sqlite_vec(conn):
        return []
    query_vector = backend.embed_one(query)
    rows = conn.execute(
        "SELECT email_id, distance FROM emails_vec WHERE embedding MATCH ? "
        "ORDER BY distance LIMIT ?",
        (_pack_vector(query_vector), limit),
    ).fetchall()
    return [(r["email_id"], r["distance"]) for r in rows]


def semantic_search(
    conn: sqlite3.Connection,
    backend: EmbeddingBackend,
    query: str,
    *,
    limit: int = 25,
    offset: int = 0,
) -> SearchResult:
    """Pure vector KNN search (mode=semantic, no keyword fusion)."""
    start = time.monotonic()
    candidates = _vector_candidates(conn, backend, query, limit=offset + limit)
    page = candidates[offset : offset + limit]
    hits = _hits_for_ids(conn, [(eid, -dist) for eid, dist in page])
    return SearchResult(
        query=query,
        mode=SearchMode.semantic,
        scope=SearchScope.all,
        total_estimated=len(candidates),
        returned=len(hits),
        offset=offset,
        hits=hits,
        timing_ms=(time.monotonic() - start) * 1000,
    )


def hybrid_search(
    conn: sqlite3.Connection,
    backend: EmbeddingBackend,
    query: str,
    *,
    scope: SearchScope = SearchScope.all,
    account: str | None = None,
    mailbox: str | None = None,
    before: int | None = None,
    after: int | None = None,
    unread_only: bool = False,
    flagged_only: bool = False,
    flag_color: str | None = None,
    has_attachments: bool | None = None,
    limit: int = 25,
    offset: int = 0,
    highlight: bool = True,
) -> SearchResult:
    """BM25 + vector KNN fused with Reciprocal Rank Fusion (k=60)."""
    start = time.monotonic()

    kw_result = FTS5Backend(conn).search(
        query,
        scope=scope,
        account=account,
        mailbox=mailbox,
        before=before,
        after=after,
        unread_only=unread_only,
        flagged_only=flagged_only,
        flag_color=flag_color,
        has_attachments=has_attachments,
        limit=CANDIDATE_N,
        offset=0,
        highlight=highlight,
    )
    vec_candidates = _vector_candidates(conn, backend, query, limit=CANDIDATE_N)

    rrf_scores: dict[str, float] = {}
    hit_by_id: dict[str, SearchHit] = {}
    for rank, hit in enumerate(kw_result.hits):
        mid = hit.message_ref.message_id
        rrf_scores[mid] = rrf_scores.get(mid, 0.0) + 1.0 / (RRF_K + rank + 1)
        hit_by_id[mid] = hit

    if vec_candidates:
        vec_hits = _hits_for_ids(conn, vec_candidates, account=account, mailbox=mailbox)
        for rank, hit in enumerate(vec_hits):
            mid = hit.message_ref.message_id
            rrf_scores[mid] = rrf_scores.get(mid, 0.0) + 1.0 / (RRF_K + rank + 1)
            hit_by_id.setdefault(mid, hit)

    ordered = sorted(rrf_scores.items(), key=lambda kv: kv[1], reverse=True)
    page = ordered[offset : offset + limit]
    hits = []
    for mid, score in page:
        hit = hit_by_id[mid]
        hit.score = score
        hits.append(hit)

    return SearchResult(
        query=query,
        mode=SearchMode.hybrid,
        scope=scope,
        total_estimated=len(rrf_scores),
        returned=len(hits),
        offset=offset,
        hits=hits,
        timing_ms=(time.monotonic() - start) * 1000,
    )


def _hits_for_ids(
    conn: sqlite3.Connection,
    id_score_pairs: list[tuple[int, float]],
    *,
    account: str | None = None,
    mailbox: str | None = None,
) -> list[SearchHit]:
    if not id_score_pairs:
        return []
    ids = [eid for eid, _ in id_score_pairs]
    scores = dict(id_score_pairs)
    placeholders = ",".join("?" for _ in ids)
    where = [f"id IN ({placeholders})"]
    params: list = list(ids)
    if account:
        where.append("(account_uuid = ? OR account_name = ?)")
        params.extend([account, account])
    if mailbox:
        where.append("(mailbox_name = ? OR mailbox_role = ?)")
        params.extend([mailbox, mailbox])
    rows = conn.execute(f"SELECT * FROM emails WHERE {' AND '.join(where)}", params).fetchall()
    rows_by_id = {r["id"]: r for r in rows}

    hits = []
    for email_id, _ in id_score_pairs:
        row = rows_by_id.get(email_id)
        if row is None:
            continue
        hits.append(summary_to_hit(row_to_summary(row), scores[email_id]))
    return hits
