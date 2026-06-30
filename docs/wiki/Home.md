---
covers: []
last_verified: 2026-06-30
---

# cobos-apple-mail-mcp Wiki

Unified Apple Mail MCP server — fast on-disk reads/search plus complete AppleScript writes,
behind one safety layer. Author: Ernesto Cobos. License: GPL-3.0-or-later.

This Wiki is the deep documentation; the [README](https://github.com/ErnestoCobos/cobos-apple-mail-mcp#readme)
is the quickstart and pitch. [CLAUDE.md](https://github.com/ErnestoCobos/cobos-apple-mail-mcp/blob/main/CLAUDE.md)
is the fast-routing index for anyone (human or agent) working on the code itself.

## Pages

- **[Architecture](Architecture.md)** — the dual-path design, the diagram, the read→write flow,
  module map.
- **[Apple Mail on-disk format](Apple-Mail-on-disk-format.md)** — Envelope Index schema,
  `.emlx`/`.partial.emlx` layout, ROWID↔Message-ID mapping, Cocoa-epoch timestamps, version
  directories.
- **[Identity & resolution](Identity-and-resolution.md)** — the canonical-id design, the
  resolver algorithm, `MultipleMatches`, the `resolve_cache`.
- **[Safety, confirmation & undo](Safety-confirmation-and-undo.md)** — `guard()`, batch caps,
  dry_run/confirm, the undo journal and its honest limits.
- **[Indexing and watch](Indexing-and-watch.md)** — the inventory-diff algorithm, crash-safe
  bulk build, the `--watch` loop, dead-letter handling, staleness.
- **[Search](Search.md)** — the FTS5 schema, BM25 weights, scopes, the `SearchBackend` seam,
  trigram, hybrid/semantic search.
- **[Threading and knowledge](Threading-and-knowledge.md)** — JWZ threading, the
  awaiting-reply/needs-response heuristics, analytics.
- **[Tools reference](Tools-reference.md)** — every tool's parameters, output shape, and backend.
- **[Resources and prompts-recipes](Resources-and-prompts-recipes.md)** — the `email://...`
  resources and how to author/run a recipe.
- **[Configuration reference](Configuration-reference.md)** — `config.toml`, `APPLE_MAIL_*` env
  vars, precedence, every setting.
- **[Permissions and troubleshooting](Permissions-and-troubleshooting.md)** — Full Disk Access,
  Automation, common errors.
- **[Single-file packaging](Single-file-packaging.md)** — building and running
  `apple-mail-mcp.pyz`.
- **[Install per client](Install-per-client.md)** — Claude Desktop/Cowork, Codex, Kimi, plus the
  MCP Inspector.
- **[Performance and benchmarks](Performance-and-benchmarks.md)** — methodology and numbers.
- **[Development and contributing](Development-and-contributing.md)** — testing without a Mac,
  CI, release process.

## Knowledge map (subsystem → source → page)

The authoritative copy of this table lives in
[CLAUDE.md](https://github.com/ErnestoCobos/cobos-apple-mail-mcp/blob/main/CLAUDE.md) — it's the
fast routing index used when working on the code; this Wiki is where each row's page actually
lives.
