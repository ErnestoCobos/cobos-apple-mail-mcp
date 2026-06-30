---
covers: []
last_verified: 2026-06-30
---

# Performance & benchmarks

## What's actually been measured vs. what's a design target

Honesty matters more than impressive numbers here. During development, every timing claim was
verified against **synthetic `.emlx` fixtures** (a handful to a few dozen messages) — real,
working code paths, but not a stress test against a real multi-hundred-thousand-message mailbox.
The numbers below are split accordingly.

### Measured (synthetic fixtures, this machine)

- `search()` (`SearchResult.timing_ms`) on a handful of indexed messages: consistently
  sub-millisecond to low-single-digit milliseconds — e.g. `0.24ms`–`0.30ms` observed in
  end-to-end MCP tool-call tests.
- `build_index(full=True)` on 1-2 messages: ~3ms (`IndexBuildResult.duration_sec`).
- `--read-only` blocking a write tool: ~1-6ms (a real regression was caught and fixed here — an
  earlier version took **~20 seconds** because it resolved the message via JXA before checking
  `--read-only`; see [Safety, confirmation & undo](Safety-confirmation-and-undo.md)).

### Design targets (informed by the architecture, not yet stress-tested at scale)

- Full-mailbox `search`: sub-100ms BM25-ranked at up to roughly a million indexed messages — this
  is FTS5's documented characteristic at this scale, not a number this project measured directly
  (no test mailbox of that size was available during development). See
  [RESEARCH.md](https://github.com/ErnestoCobos/cobos-apple-mail-mcp/blob/main/RESEARCH.md) for
  the comparison against alternative search engines that led to this expectation.
- Index build: roughly 30x faster than scripting Mail.app for the same scan, because indexing
  never launches AppleScript — it's a filesystem walk + an immutable SQLite read. This ratio
  comes from the architectural difference (process-per-AppleScript-call vs. direct file I/O),
  not a head-to-head benchmark run in this repo.
- `--watch` latency: new mail typically reflected within a couple of seconds, bounded by the
  500ms debounce window plus indexing time for the batch.

### Verified for real (not synthetic): the `osascript` subprocess mechanics

`tests/test_jxa_executor.py` runs real `osascript` calls (no Mail.app interaction — scripts that
don't touch `Application("Mail")`, so no Automation permission needed) to verify the timeout/
process-group-kill behavior actually works: a deliberately hung script (`while (true) {}`) is
killed and returns control within the configured timeout, not Apple Events' own ~2-minute
default wait.

## Running your own benchmark

```bash
apple-mail-mcp index build --full --verbose
apple-mail-mcp index status        # total_indexed, dead_letter_count, embed coverage
time apple-mail-mcp search "some term" --highlight
```

If you benchmark against your own mailbox and the numbers differ meaningfully from the design
targets above, that's genuinely useful signal — please open an issue with `index status` output
and mailbox size.
