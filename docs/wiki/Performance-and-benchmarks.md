---
covers: []
last_verified: 2026-06-30
---

# Performance & benchmarks

## What's actually been measured vs. what's a design target

Honesty matters more than impressive numbers here. Early development timing claims were verified
only against **synthetic `.emlx` fixtures** (a handful to a few dozen messages); those code paths
were real but the numbers were extrapolated, not stress-tested. That gap has since been closed —
below are real measurements against a **real 7-account, 210,152-message mailbox**
(`apple-mail-mcp index build --full` run to completion, then queried live), alongside what's
still a synthetic-only or extrapolated number.

### Measured against a real 210,152-message mailbox

- **Full-mailbox `search`, realistic/selective query** (`"invoice"`, or `"meeting"` scoped to
  subject): **9-20ms**, BM25-ranked. This is the common case, and confirms the sub-100ms design
  target — for queries that actually narrow the corpus.
- **Full-mailbox `search`, deliberately non-selective single-word query** (`"the"`, matching
  ~82,900 of 210,152 messages — 39% of the whole mailbox): **0.3-1.6s**, not sub-100ms. Expected
  FTS5/BM25 behavior when a query barely narrows the candidate set at all — ranking tens of
  thousands of rows before returning the top page is inherently more expensive than ranking a
  few thousand. Real-world queries are essentially never this unselective, but the unqualified
  "sub-100ms at any scale" claim from before this measurement was wrong and has been corrected.
- **First full index build**: 210,152 messages in 679.6s (`IndexBuildResult.duration_sec`) — about
  3.2ms/message, one-time, includes HTML→text conversion and a full JWZ re-thread. `failed: 0` —
  see [Apple Mail on-disk format](https://github.com/ErnestoCobos/cobos-apple-mail-mcp/wiki/Apple-Mail-on-disk-format) for the malformed-header
  sanitization that made this possible; the first attempt against this same mailbox crashed
  partway through before that fix.
- `get_inbox_overview`, `get_needs_response`, `get_email_thread`: all sub-second, computed
  entirely from the local index with zero Mail.app/AppleScript involvement.
- `list_accounts`/`account` fields on every read tool: currently the raw account UUID, not a
  human display name — see [Tools reference](https://github.com/ErnestoCobos/cobos-apple-mail-mcp/wiki/Tools-reference) for why and what would fix it.

### Measured (synthetic fixtures, low message counts)

- `search()` (`SearchResult.timing_ms`) on a handful of indexed messages: consistently
  sub-millisecond to low-single-digit milliseconds — e.g. `0.24ms`–`0.30ms` observed in
  end-to-end MCP tool-call tests. Consistent with the low-thousands-of-messages case of the real
  mailbox above.
- `--read-only` blocking a write tool: ~1-6ms (a real regression was caught and fixed here — an
  earlier version took **~20 seconds** because it resolved the message via JXA before checking
  `--read-only`; see [Safety, confirmation & undo](https://github.com/ErnestoCobos/cobos-apple-mail-mcp/wiki/Safety-confirmation-and-undo)).

### Still a design target, not directly measured

- Index build vs. scripting Mail.app for the same scan: this project has not run a literal
  head-to-head against an AppleScript-based scanner. The ~3.2ms/message figure above is real; the
  claim that this beats AppleScript scripting by roughly an order of magnitude or more rests on
  the architectural difference (one process-per-AppleScript-call vs. direct file I/O in a single
  process) rather than a benchmark run in this repo.
- `--watch` latency: new mail typically reflected within a couple of seconds, bounded by the
  500ms debounce window plus indexing time for the batch — not yet measured against sustained
  real mail arrival over time.

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
