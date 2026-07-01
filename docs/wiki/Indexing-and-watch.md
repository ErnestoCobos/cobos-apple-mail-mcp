---
covers:
  - src/cobos_apple_mail_mcp/read/indexer.py
  - src/cobos_apple_mail_mcp/read/watcher.py
last_verified: 2026-06-30
---

# Indexing and `--watch`

## The key trick: filename == ROWID

A `.emlx` filename's numeric stem *is* the Envelope Index ROWID, so the indexer can diff
filesystem path sets (mtime, size) against what's already in `emails` to classify
added/changed/deleted/**moved** without re-parsing every file:

```
disk  = {path: (mtime, size) for path in walk(MAIL_DIR, "**/*.emlx")}
db    = {row.emlx_path: (row.emlx_mtime, row.emlx_size) for row in SELECT ... FROM emails}

added   = disk_paths - db_paths
deleted = db_paths - disk_paths
changed = {p for p in disk_paths & db_paths if disk[p] != db[p]}

# a delete+add pair sharing the same ROWID stem is a MOVE, not a reparse:
moved = [(old_path, new_entry) for entries sharing rowid in (added ∩ deleted by rowid)]
```

Implemented in `read/indexer.py::inventory_diff()`. A move only updates `emlx_path`,
`account_uuid`, `mailbox_name`/`role` — no reparse needed.

## Crash-safe bulk build sequence

`build_index(conn, mail_dir, full=False, enable_trigram=False)`:

1. `inventory_diff()`.
2. Resolve account display names (`read/account_names.py::resolve_account_names()`, see
   [Apple Mail on-disk format](https://github.com/ErnestoCobos/cobos-apple-mail-mcp/wiki/Apple-Mail-on-disk-format#account-display-names))
   and backfill any already-indexed row whose `account_name` doesn't match yet
   (`_backfill_account_names()`) — a cheap indexed `UPDATE`, not a reparse, so it runs on every
   build, not just `--full`.
3. If `full`: drop the FTS5 triggers (so per-row inserts during the bulk parse don't also fire
   small FTS writes — batched at the end instead).
4. Parse + UPSERT `added`/`changed` entries in batches of 500
   (`read/indexer.py::_index_entries()`); each batch commits independently, so a crashed build
   resumes cleanly — the `(mtime, size)`-gated diff just re-discovers whatever wasn't committed
   yet. A path that fails to parse is recorded in `failed_index_jobs` (dead-letter) **and a path
   that previously failed but now parses successfully has its dead-letter entry cleared** — a
   real bug caught during development (the table only ever grew until this fix).
   `_flush_batch()` isolates a bad row two ways: `emlx_parser.py::_sanitize_parsed()` strips lone
   UTF-16 surrogates that `email.policy.default`'s lenient header decoding can leave in place for
   malformed/non-UTF-8 real-world headers (sqlite3 rejects those with `UnicodeEncodeError`), and
   if anything still slips through, the batch `executemany` falls back to one row at a time,
   dead-lettering only the offending row instead of losing — or aborting the build on — the whole
   500-row batch. Found running a full build against a real 209k-message, multi-account mailbox
   for the first time, which is exactly the scale where years of varied, occasionally malformed
   mail actually shows up; synthetic fixtures never hit this.
5. Apply `deleted`/`moved`.
6. If `full`: rebuild `emails_fts` (delete + `INSERT...SELECT`, **not** the bare `'rebuild'`
   command — see [Search](https://github.com/ErnestoCobos/cobos-apple-mail-mcp/wiki/Search) for why), recreate the triggers, optimize, and rebuild the
   trigram table if `enable_trigram`.
7. If anything changed: recompute JWZ threading for the whole index (`read/threader.py::
   index_threads()` — cheap enough at personal-mailbox scale to just rerun, see that page).
8. Record `sync_state` (`last_build`/`last_full_build`, `mail_dir`, `envelope_mtime`).

## `--watch`

`read/watcher.py::run_watch_loop()` reacts to filesystem events via `watchfiles` (Rust/fsevents,
debounced 500ms) and reindexes via the **same** `build_index(full=False)` path used by `index
build` — no separate incremental-update code path to keep in sync. Each tick:

- Calls `build_index`, catching and logging any exception rather than killing the loop.
- Runs `('optimize')` on the FTS index every ~2000 accumulated changes.
- If `config.embeddings.enabled`, drains a couple of small batches of the embedding backfill
  queue at low priority (`read/vector_search.py::embed_backfill(max_batches=2)`) — bounded so it
  never blocks the indexer from reacting to new mail.
- Records `sync_state["last_watch_tick"]`.

If `watchfiles` isn't installed (the `[watch]` extra), the loop **degrades to periodic polling**
(`inventory_diff()` every 30s) rather than failing — consistent with this project's "lazy/guarded
optional dependency" rule.

A parse race (Mail.app mid-write when the indexer reads a `.emlx`) just dead-letters that one
path for this tick; it clears itself automatically once the file is stable on a later tick.

## Staleness

`index status` (`read/indexer.py::get_index_status()`) reports `total_indexed`,
`pending_added/changed/deleted` (a fresh `inventory_diff()` against the live filesystem),
`dead_letter_count`, `embed_total`/`embed_done`, and `stale: bool` — stale if there's any pending
change, or if the last build/watch tick is older than `config.index.staleness_hours` (default
24).
