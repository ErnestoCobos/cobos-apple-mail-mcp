---
covers:
  - src/cobos_apple_mail_mcp/read/search.py
  - src/cobos_apple_mail_mcp/read/vector_search.py
last_verified: 2026-06-30
---

# Search

## Why FTS5 (and what was actually compared)

FTS5 was independently re-verified against current alternatives for this exact architecture
(embedded, single-user, no daemon, hybrid-ready, incremental updates) — not chosen by default.
See [RESEARCH.md](https://github.com/ErnestoCobos/cobos-apple-mail-mcp/blob/main/RESEARCH.md#3-fast-search--understanding-the-mailbox--state-of-the-art)
for the full comparison against Tantivy, DuckDB, Spotlight, and vector-native stores. Search is
wrapped behind `read/search.py::SearchBackend` so a different engine could slot in later without
touching `tools/search_tools.py`.

## The external-content footgun (and the fix)

A real correctness bug was found and fixed while building this: FTS5's **external-content**
mode (which avoids duplicating body text by reading it from a separate content table) makes
`snippet()`/`highlight()` and the bare `INSERT INTO fts(fts) VALUES('rebuild')` command fail with
`"no such column"` whenever the FTS5 column names don't exactly match the content table's column
names. This project's `emails` table composes the FTS5 `sender` column from
`sender_name`+`sender_addr` (and `recipients`/`attachments` similarly), so external-content mode
silently broke highlighting.

**Fix**: `storage/migrations.py::EMAILS_FTS_DDL` defines `emails_fts` as a **self-contained**
(non-external-content) FTS5 table that stores its own copy of the searchable text. This costs
some extra storage but makes `snippet()`/`highlight()` work correctly, and simplifies the sync
triggers — a plain `DELETE FROM emails_fts WHERE rowid=?` works (no special `('delete', ...)`
form needed), and a full rebuild is a plain `DELETE` + `INSERT...SELECT` instead of the
column-name-fragile `'rebuild'` command.

## Schema

```sql
CREATE VIRTUAL TABLE emails_fts USING fts5(
  subject, sender, recipients, body, attachments,
  tokenize='porter unicode61 remove_diacritics 2',
  prefix='2 3 4'
);
```

Sync triggers (`storage/migrations.py::FTS_TRIGGERS_SQL`) fire on every `INSERT`/`UPDATE`/
`DELETE` on `emails`, except during a full index build, where they're dropped and the table is
bulk-repopulated once at the end (`read/indexer.py::_rebuild_fts_index()`).

## `search()` API

```
search(query, scope ∈ {all,subject,sender,body,attachments},
       mode ∈ {keyword,semantic,hybrid} = keyword,
       account, mailbox, before, after, unread_only, flagged_only, has_attachments,
       limit=25, offset=0, highlight=True)
```

`scope` maps to an FTS5 column filter (`subject:(...)`, etc.) — `all` searches every column
unfiltered. All other filters (account, mailbox, date range, unread/flagged/has_attachments) are
plain SQL `WHERE` clauses joined against `emails`, **never** folded into the FTS5 `MATCH`
expression — keeps user input out of FTS5 query syntax entirely
(`core/text.py::sanitize_fts_query()` additionally escapes anything that looks like it).

BM25 weights (`read/search.py::_BM25_WEIGHTS = "10.0, 8.0, 4.0, 1.0, 3.0"`) — subject and sender
matches outrank an incidental body mention.

## Trigram substring search (optional)

`config.index.enable_trigram` builds a second, self-contained FTS5 table
(`emails_trgm`, `tokenize='trigram'`) for substring queries (partial filenames, `@domain`
fragments) that porter-stemmed search misses. Only refreshed on a full rebuild (`index rebuild`),
not on every `--watch` tick — an accepted lag for an opt-in fallback. `tools/search_tools.py`
falls back to it automatically when keyword search returns zero hits and the query
`looks_substring_query()` (no spaces, contains `.@_-`). If the table hasn't been built yet (flag
just turned on, no rebuild run), `TrigramBackend.search()` catches the resulting
`sqlite3.OperationalError` and returns no results rather than crashing.

## Hybrid / semantic search (optional `[semantic]` extra)

Off by default (`config.embeddings.enabled = false`). When enabled:

- **Default backend: Apple's `NaturalLanguage` framework** (`NLEmbedding`) via PyObjC — built
  into macOS, no model download, ~512-dimensional on-device sentence embeddings. Chosen
  specifically as the most resource-frugal option, per the project's design priority.
- **Fallback: MiniLM** (ONNX, 384-dim) — requires the `[semantic-minilm]` extra and a local model
  directory (`model.onnx` + `tokenizer.json`); no auto-download, to keep this layer local-only
  and predictable.
- Vectors are stored in a `sqlite-vec` `vec0` table (`emails_vec`), dimension matching the active
  backend — **a backend change invalidates previously-embedded rows** rather than mixing vectors
  from two different spaces (`read/vector_search.py::sync_vec_table()`).
- `mode=hybrid` fuses BM25 candidates and vector-KNN candidates with **Reciprocal Rank Fusion**
  (k=60) — no cross-scale score normalization needed, just rank position in each list.
- Every backend call is defensively wrapped (`try`/`except Exception`) — a PyObjC binding
  mismatch or missing ONNX model degrades to "unavailable" (search falls back to keyword mode
  with `degraded: true` in the response), never a crash.
- **Verified for real on this machine** (not just compile-checked): `AppleNLBackend.is_available()`
  returned `True`, embeddings came back as genuine 512-dimensional vectors, and a real semantic
  query using words that never appeared in the indexed emails ("money and finances discussion")
  correctly ranked the financially-themed email first by actual semantic similarity — true
  understanding, not keyword overlap. This is the one part of this project that carried the most
  integration-risk uncertainty during design (PyObjC bridging to a framework with limited public
  Python documentation), and it works as designed.
- The embedding backfill (`embed_backfill()`) runs in small batches from the `--watch` loop at
  low priority, never blocking indexing.

See [RESEARCH.md](https://github.com/ErnestoCobos/cobos-apple-mail-mcp/blob/main/RESEARCH.md) for
why Apple NL was chosen over bundling MiniLM by default.
