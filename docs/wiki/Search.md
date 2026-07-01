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

### The ranking formula, and why it explains the measured timings

FTS5's `bm25()` is the standard Okapi BM25, summed per column and scaled by the weight vector
above (*c* ranges over `subject, sender, recipients, body, attachments`; *k1*=1.2, *b*=0.75 are
FTS5's defaults):

```
score(D, Q) = Σ_c  w_c · Σ_{t∈Q}  IDF(t) · f_c(t,D)·(k1+1)
                                  ────────────────────────────────
                                  f_c(t,D) + k1·(1 − b + b·|D_c|/avgdl_c)

IDF(t) = ln( (N − n(t) + 0.5) / (n(t) + 0.5) )
```

where *N* = total indexed messages, *n(t)* = messages containing term *t*, *f_c(t,D)* = how many
times *t* appears in document *D*'s column *c*, and `|D_c|`/`avgdl_c` are that column's length and
average length. The part that matters for **performance**, not just ranking quality, is *n(t)*:
it sets the size of the candidate set FTS5 has to score before it can return a top-*K* page — a
`O(m log K)` operation for *m* candidates (a bounded min-heap of size *K*, not a full sort).

This is exactly what a real 210,152-message mailbox measured (see
[Performance and benchmarks](https://github.com/ErnestoCobos/cobos-apple-mail-mcp/wiki/Performance-and-benchmarks)):

| Query | *n(t)* (candidates) | IDF(t) | measured |
|---|---|---|---|
| `"invoice"` | 2,745 | ln(207,407.5 / 2,745.5) ≈ **4.32** | 19.8ms |
| `"the"` | 82,893 | ln(127,259.5 / 82,893.5) ≈ **0.43** | 289.6ms–1.6s |

Two things fall out of the same *n(t)* in one step: `"invoice"`'s IDF is ~10× larger (a rarer
term is more informative — the textbook reason IDF exists), *and* its candidate set is ~30×
smaller (fewer documents to run through the `O(m log K)` scorer — the reason it's also ~15–80×
faster in practice). A single-word, highly common query is the pathological case for *both* axes
at once, which is exactly the "the" row above: technically correct, cheaply computed per-document,
just applied to nearly 40% of the entire mailbox before the top page can be selected.

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
- `mode=hybrid` fuses BM25 candidates and vector-KNN candidates with **Reciprocal Rank Fusion**:

  ```
  RRF(d) = Σ_r  1 / (k + rank_r(d))
  ```

  summed over each ranker *r* (BM25 keyword search, vector cosine-similarity KNN) that returned
  document *d*; `rank_r(d)` is *d*'s 1-indexed position in that ranker's list, and *k=60* (the
  constant from Cormack et al.'s original RRF paper — large enough that rank 1 vs. rank 2 in one
  list doesn't dominate the fused score, small enough that being highly ranked still matters). The
  entire appeal of RRF here is that it needs **no score normalization** between two numerically
  incomparable scales (BM25's unbounded relevance score vs. cosine similarity's [-1, 1]) — only
  rank position, which both rankers produce for free:

  ```mermaid
  flowchart LR
      Q["query"] --> BM["BM25 (FTS5)<br/>top-K by keyword"]
      Q --> KNN["vector KNN (sqlite-vec)<br/>top-K by cosine similarity"]
      BM --> RRF{{"RRF(d) = Σ 1/(k+rank)<br/>k = 60"}}
      KNN --> RRF
      RRF --> OUT["fused, re-ranked results"]
  ```

  Fusing two already-small top-*K* lists (`limit`, default 25) is `O(K)` to merge by document id
  plus `O(K log K)` to re-sort by the fused score — negligible next to either ranker's own search
  cost, so `mode=hybrid` is never meaningfully slower than running the two searches separately.
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
