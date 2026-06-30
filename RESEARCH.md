# RESEARCH.md — Phase 0 findings

Author: Ernesto Cobos. This document records the research that shaped
`cobos-apple-mail-mcp`'s architecture: a map of the two upstream projects
being merged, Apple Mail's on-disk format, and the state of the art for
fast email search and MCP server design — followed by a prioritized list
of improvement opportunities and which ones this project actually builds.

## 1. The two upstream projects

### imdinu/apple-mail-mcp (GPL-3.0) — fast reads, read-only

- FastMCP-based, Python 3.11+. Reads Apple Mail's `Envelope Index` SQLite
  database and `.emlx` files directly from disk (~3–7ms per operation),
  plus maintains its own FTS5 full-text index for full-mailbox body search
  (~2ms BM25-ranked queries) — far faster than scripting Mail.app.
- 8 tools: `list_accounts`, `list_mailboxes`, `get_emails`, `get_email`,
  `search`, `get_email_links`, `get_email_attachment`, plus a deprecated
  `get_attachment` alias. No write tools.
- Indexing infrastructure: `IndexManager` (full build + incremental sync),
  `IndexWatcher` (real-time `.emlx` change detection via `watchfiles`),
  dead-letter table for parse failures, JXA fallback for correctness when
  disk access fails.
- Influence on this project: the entire direct-on-disk read architecture —
  `read/envelope_reader.py`, `read/emlx_parser.py`, `read/indexer.py`,
  `read/watcher.py`, the FTS5 schema design — is informed by this
  approach. Because it's GPL-3.0, the combined work here is
  GPL-3.0-or-later (see NOTICE).

### patrickfreyer/apple-mail-mcp (MIT) — full read-write via AppleScript/JXA

- FastMCP-based, Python 3.10+. ~22–24 tools across inbox ops, search,
  compose, drafts, management (move/status/trash/mailboxes), and analytics
  (statistics, awaiting-reply, needs-response, top-senders), plus an
  optional MCP-UI dashboard.
- All reads and writes go through AppleScript/JXA — correct (it's the
  documented automation surface for Mail.app) but slow (seconds, not
  milliseconds) for anything read-heavy.
- Critical limitation found during research: write tools locate their
  target message primarily by **`subject_keyword` substring matching**
  against the live mailbox. This is fragile and ambiguous — a substring
  match can hit the wrong message, especially in a thread with repeated
  subjects ("Re: Re: Budget", "Re: Budget", "Budget") or across multiple
  accounts. This is the single most important problem this project's
  identity/resolution design (`core/resolver.py`) was built to fix.
- Already-good safety patterns worth keeping: conservative batch defaults
  (move 1, status 10, trash 5), a `dry_run` parameter on destructive
  operations, and basic output-path validation for exports/attachments
  (blocking `.ssh`, `.gnupg`, `.aws`, etc.) — all carried forward and
  formalized into `core/safety.py::guard()` and `core/paths.py`.

### Overlap & the merge decision

Both projects implement `list_accounts`/`list_mailboxes`/some form of
`search`/`get_emails`. The merge keeps imdinu's disk-first implementation
for every read tool (1000x faster for the same data) and keeps
patrickfreyer's AppleScript/JXA approach for every write tool (the only
correct way to make Mail.app send/move/etc.), rewriting the write layer's
message-targeting to resolve by canonical Message-ID instead of subject
substring matching.

## 2. Apple Mail's on-disk format

### Location & versioning

`~/Library/Mail/V{N}/` (N has been 8 for Big Sur, 9 for Monterey, 10 for
Ventura/Sonoma/Sequoia — multiple version directories can coexist on a
system upgraded across releases; the highest number is current).
`read/envelope_reader.py::find_mail_directory()` picks the highest `V{N}`.

```
~/Library/Mail/V10/
├── MailData/
│   ├── Envelope Index          (SQLite3 — message metadata)
│   ├── Envelope Index-wal/-shm (WAL files — never touched by this project)
│   └── ...
└── {Account-UUID}/             (one folder per account, UUID-named)
    └── {Mailbox}.mbox/
        └── 0/0/                (partition dirs)
            ├── Messages/
            │   ├── {ROWID}.emlx
            │   └── {ROWID}.partial.emlx
            └── Attachments/{ROWID}/{n}/{filename}
```

### The Envelope Index SQLite database

A reverse-engineered (undocumented) schema. Key tables: `messages`
(ROWID, date_received, date_sent, flags, sender, subject, mailbox FKs),
`addresses`, `subjects`, `mailboxes`, `recipients`. Timestamps are **Cocoa
epoch** (seconds since 2001-01-01T00:00:00Z, not Unix epoch — offset
978307200; see `read/envelope_reader.py::apple_to_unix()`).

Because the exact column set has shifted across macOS releases and is
undocumented, this project treats the Envelope Index as a **best-effort
supplementary source only** (`read/envelope_reader.py::read_envelope_flags()`,
defensive `PRAGMA table_info` introspection) — never required. The
authoritative source for every field this project needs is the `.emlx`
file itself, which has a stable, documented structure (below) and contains
the same information (headers, flags) independent of any SQLite schema
drift.

**Safety**: the Envelope Index is opened `file:...?immutable=1` — never
written, and immutable mode sidesteps SQLite's locking protocol entirely
rather than racing Mail.app for a lock.

### The `.emlx` file format

```
<byte-count>\n<RFC822 message><XML plist trailer>
```

The first line is a decimal byte count covering exactly the RFC822
message that follows; whatever comes after is an Apple property list with
supplementary metadata — `flags` (bitfield: bit0=Seen/read, bit1=Answered,
bit2=Flagged, bit3=Deleted, bit4=Draft), `date-sent`, `date-received`,
`remote-id`. `.partial.emlx` files contain headers only — the body's
attachments live in a sibling `Attachments/{ROWID}/{n}/` tree instead of
being inlined (`read/emlx_parser.py` handles both layouts).

### The identity bridge: ROWID, Message-ID, and Mail's internal id

This is the crux of the whole write-layer design. Three distinct
identifiers exist for the same message:

1. **Envelope Index `ROWID`** — identical to the `.emlx` filename's
   numeric stem. Fast, but **not stable across an Envelope Index
   rebuild** (Mail can rebuild this database, reassigning ROWIDs).
2. **RFC822 `Message-ID` header** — globally unique, permanent, present in
   both the `.emlx` plist and (for live messages) Mail's own object model.
3. **Mail's internal integer id** (JXA `message.id()`) — distinct from
   both of the above; JXA separately exposes `message.messageId()`, which
   *is* the RFC822 Message-ID string (with angle brackets), queryable via
   `messages whose message id is "<...>"`.

This project uses the **normalized RFC822 Message-ID** as the canonical id
exposed to MCP clients (`core/identity.py`), because it's the one value
obtainable from both the disk-read path (the `.emlx` plist / parsed
headers) and the live AppleScript/JXA path. Drafts and other mail with no
usable Message-ID get an opaque `amid:` handle instead (see
`core/identity.py::make_opaque_handle()`).

A second, non-obvious gap found during design: a Mail account's on-disk
**directory UUID** has no guaranteed relationship to the **JXA-visible
account name** Mail.app understands for scripting. `core/resolver.py`
bridges the two via account-name / email-address heuristics, scoped in
priority order (caller hint → `resolve_cache` → the read layer's own seed
of sender-address + mailbox-name → a bounded broad scan), with mandatory
read-back verification before any mutation — see
[Identity & resolution](docs/wiki/Identity-and-resolution.md).

## 3. Fast search & "understanding the mailbox" — state of the art

### SQLite FTS5: chosen, and verified to still be the right choice

FTS5 was independently re-validated against current alternatives
(Tantivy/Rust, DuckDB FTS, Meilisearch/Typesense, native Spotlight,
vector-native stores) for this specific architecture — embedded,
single-user, no daemon, hybrid-ready, incremental updates. None of them
won outright:

- **Tantivy** is the only real contender on raw speed (~30–80ms vs.
  FTS5's ~50–100ms at ~1M emails) but requires a Rust wheel and a
  *separate* index store, breaking the clean coexistence with `sqlite-vec`
  in one file — not worth it unless real-world profiling proves FTS5 is a
  bottleneck (unlikely at personal-mailbox scale).
- **DuckDB** is OLAP-oriented and a poor fit for the `--watch` incremental
  insert pattern, though it would be a fine *analytics* backend if this
  project ever needed it (it doesn't — plain aggregate SQL over
  `index.db` is enough).
- **Spotlight/`mdfind`** already indexes Mail but offers no ranking
  control, no reliable scoping, and no guarantee of indexing custom data —
  unsuitable as the primary engine.
- **Vector-native stores** (LanceDB, Qdrant) don't replace keyword search;
  `sqlite-vec` exists specifically to complement FTS5, which is the
  pattern this project uses.

A real correctness pitfall discovered (and fixed) while implementing this:
FTS5's **external-content** mode — which avoids duplicating body text —
makes `snippet()`/`highlight()` and the bare `'rebuild'` command fail
("no such column") whenever the FTS5 column names don't exactly match the
content table's column names. This project's `emails` table composes
`sender` from `sender_name`+`sender_addr` and similar, so external-content
mode silently breaks highlighting. The fix: `emails_fts` is a
**self-contained** (non-external-content) FTS5 table that stores its own
copy of the searchable text — a small storage cost, fully correct
`snippet()`/`highlight()`, and a simpler trigger design (a plain `DELETE
FROM emails_fts WHERE rowid=?` works, no special `('delete', ...)` form
needed). See `storage/migrations.py::EMAILS_FTS_DDL` for the full
rationale.

Search is wrapped behind a `SearchBackend` interface
(`read/search.py::FTS5Backend`) specifically so a different engine could
slot in later without touching `tools/search_tools.py`.

### Hybrid keyword + semantic search

Reciprocal Rank Fusion (RRF, k=60) is the standard, scale-free way to
combine BM25 results with vector KNN results — no cross-scale score
normalization needed, only rank position in each list
(`read/vector_search.py::hybrid_search()`).

For the embedding model itself, the user's explicit preference ("the
least resource-demanding approach") was decisive: **Apple's
`NaturalLanguage` framework (`NLEmbedding`)**, reached via PyObjC, is
built into macOS — no model download, no extra runtime, ~512-dimensional
sentence embeddings, on-device. This beats bundling MiniLM (ONNX/PyTorch,
50–200MB+) as the default for this project's stated goal. MiniLM is kept
as an opt-in fallback for multilingual text or non-NL-available
environments (`read/vector_search.py::MiniLMBackend`). Both backends
import their native dependencies lazily, so the core package has zero
heavy dependencies when semantic search is disabled (the default).

This was verified for real during development, not just designed on paper:
with the `[semantic]` extra installed, `AppleNLBackend.is_available()`
returns `True` on this machine, and a genuine semantic query ("money and
finances discussion") correctly ranked a financially-themed test email
first by actual meaning rather than keyword overlap — see
[Search](docs/wiki/Search.md) for the full result. This was the one part
of the design carrying the most integration-risk uncertainty (PyObjC
bridging to a framework with limited public Python documentation), and it
works as designed.

Apple Intelligence's **Foundation Models** framework (on-device LLM,
macOS 26+) was investigated as a path to real summarization rather than
embedding-based retrieval. It's Swift-only (no PyObjC bridge), so it's
reached via a separately-built Swift helper binary
(`swift/foundation-models-summarizer/`) over a subprocess boundary — see
that directory's README for what was actually compile-verified vs. left
for the user to verify on a fully-configured Mac.

### Conversation threading & triage heuristics

**JWZ threading** (Jamie Zawinski's 1997 algorithm, still the basis of
most mail clients' thread reconstruction) builds a forest of "containers"
linked by References/In-Reply-To, with phantom containers for referenced-
but-absent messages and a subject-based fallback for mail with missing
headers — implemented in `read/threader.py`, run entirely against the
index (no `.emlx` reparse needed at query time).

"Awaiting reply" and "needs response" have no universally agreed
definition in the literature — this project uses transparent, tunable
heuristics rather than a black-box classifier: awaiting-reply scans Sent
messages for the absence of a matching In-Reply-To/References/subject
match from the recipient within a window; needs-response scores unread
inbox mail on question marks, request-phrase cues, urgency cues, and age,
filtering bulk/newsletter mail via `List-Unsubscribe`/`List-Id`/
`Precedence` headers (`knowledge/triage.py`).

### Modern MCP server design

FastMCP (Python) was used as-is for tool/resource/prompt registration.
Two SOTA patterns this project adopted beyond a plain tool list:

- **MCP resources** (`email://accounts`, `email://threads/{id}`, etc.) as
  read-only projections of the same functions backing the tools — a
  single source of truth, not a parallel implementation
  (`resources/email_resources.py`).
- **Packaged "recipes"** (à la Spark CLI's skill/recipe model) implemented
  natively as MCP **prompts** with dynamically-built function signatures
  per recipe (`skills/loader.py`), rather than inventing a parallel
  invocation channel — any MCP client gets them automatically.

## 4. Prioritized improvement opportunities (impact vs. effort)

| Opportunity | Impact | Effort | Status in this build |
|---|---|---|---|
| Direct on-disk reads (no AppleScript for reads) | Very high | Medium | **Built** — `read/*` |
| Canonical-id write resolution (replace subject matching) | Very high (correctness) | Medium | **Built** — `core/resolver.py` |
| Mandatory safety layer (read-only/batch-cap/dry-run/confirm/undo) | High | Medium | **Built** — `core/safety.py`, `core/undo.py` |
| Incremental `--watch` indexing | High | Medium | **Built** — `read/watcher.py` |
| JWZ threading | Medium-high | Low-medium | **Built** — `read/threader.py` |
| Triage heuristics (awaiting-reply, needs-response) | Medium-high | Low | **Built** — `knowledge/triage.py` |
| MCP resources + prompts/recipes | Medium | Low | **Built** — `resources/*`, `skills/*` |
| Single-file `.pyz` packaging | Medium (distribution) | Low | **Built** — `scripts/build_pyz.sh` |
| Hybrid keyword+semantic search (frugal default) | Medium | Medium | **Built, opt-in** — `read/vector_search.py` |
| On-device LLM summarization | Medium | High (Swift, separate build) | **Scaffolded, not wired to a tool** — `swift/foundation-models-summarizer/` |
| Trigram substring search | Low-medium | Low | **Built, opt-in** — `index.enable_trigram` |
| Heavier search engine (Tantivy) if ever needed | Low (no evidence needed yet) | Medium | **Not built** — seam exists (`SearchBackend`) |
| DuckDB analytics backend | Low | Medium | **Not built** — plain SQL suffices at this scale |

## Sources consulted

- imdinu/apple-mail-mcp — https://github.com/imdinu/apple-mail-mcp (GPL-3.0)
- patrickfreyer/apple-mail-mcp — https://github.com/patrickfreyer/apple-mail-mcp (MIT)
- SQLite FTS5 documentation — https://sqlite.org/fts5.html
- sqlite-vec — https://github.com/asg017/sqlite-vec
- watchfiles — https://watchfiles.helpmanual.io/
- JWZ threading — https://www.jwz.org/doc/threading.html
- Apple NaturalLanguage / NLEmbedding — Apple Developer documentation
- Apple Foundation Models framework — WWDC 2025 session, Apple Developer documentation
- Reciprocal Rank Fusion — standard hybrid-retrieval fusion technique (Cormack et al.)
- Word to the Wise Labs — Mail.app database schema notes — https://labs.wordtothewise.com/mailapp/
