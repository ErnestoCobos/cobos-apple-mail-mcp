# cobos-apple-mail-mcp — project memory

Sole author: **Ernesto Cobos** <ernesto@cobos.io> (GitHub: ErnestoCobos). License: GPL-3.0-or-later.

This file is the fast entry point into the project: commands, architecture, hard invariants, and
the knowledge map that routes any task to the one relevant Wiki page instead of a full re-read.

## Attribution rule (non-negotiable)

Every artifact in this repository — code, comments, docstrings, README, NOTICE, this file, commit
messages, changelog, issue/PR text — is authored solely by Ernesto Cobos. Never mention Claude, AI,
an assistant, or any model anywhere in this repo. Never add a `Co-Authored-By: Claude` (or any AI)
trailer to a commit. Upstream credit is limited to the two merged projects (see NOTICE): imdinu's
apple-mail-mcp (GPL-3.0) and patrickfreyer's apple-mail-mcp (MIT). This rule is self-reinforcing:
any agent or contributor reading this file must follow it without being asked again.

## Commands

```bash
uv sync --all-extras              # install (or: pip install -e ".[dev,full]")
uv run pytest                     # unit tests (no Mac/Mail required — JXA boundary is mocked)
uv run ruff check src tests       # lint
uv run apple-mail-mcp init        # generate ~/.cobos-apple-mail-mcp/config.toml
uv run apple-mail-mcp index build # build the FTS5 index from ~/Library/Mail
uv run apple-mail-mcp index status
uv run apple-mail-mcp serve       # run the MCP server (stdio)
uv run apple-mail-mcp watch       # incremental indexer
make pyz                          # build dist/apple-mail-mcp.pyz (scripts/build_pyz.sh)
scripts/publish_wiki.sh           # sync docs/wiki/ -> GitHub wiki
python scripts/check_docs_sync.py # docs drift guard (also runs in pre-commit + CI)
```

## Architecture (one paragraph)

Reads are direct-on-disk: the live `Envelope Index` SQLite is opened `immutable=1` (never written),
`.emlx` files are parsed from disk, and both feed a derived, disposable `index.db` (FTS5 keyword
search + optional vector search + JWZ threading + triage). Writes go through AppleScript/JXA via
`osascript`, always wrapped by `core.safety.guard()`. The two halves are bridged by a canonical
identity (normalized RFC822 Message-ID, or an `amid:` opaque handle for drafts) resolved through
`core.resolver` with mandatory read-back verification before any mutation — see `core/identity.py`,
`core/resolver.py`.

## Hard invariants (do not violate)

1. **Identity & resolution.** The canonical message id is the bracket-stripped, case-sensitive
   RFC822 Message-ID (`core/identity.py`). Resolving an id to a live Mail message
   (`core/resolver.py`) must scope the search (hint → cache → read-seed → bounded broad scan), fetch
   via `messages whose message id is X`, and **read the id back to verify** before any mutation. Zero
   matches → `NotFound`. More than one match on a **write** → return `MultipleMatches`; **never
   auto-pick** a message to mutate. `subject_keyword` is an explicit opt-in locator only, and even
   then resolves to a Message-ID first.
2. **Safety layer.** No write tool calls `write/*` directly — everything passes through
   `core.safety.guard()`: `read_only` blocks all send/modify tools (draft creation stays allowed);
   batch caps (`move=1, status=10, trash=5, delete=1`) are rejected, never silently truncated;
   `dry_run` returns a `Preview` with zero mutation; `permanent_delete`/`empty_trash` require
   `confirm=true` matched against the same resolved id set (`ConfirmationStale` if it drifted).
3. **Undo is honest.** Only move/trash(until emptied)/status/flag are journaled and undoable via
   `core/undo.py::undo_last`. Send/reply/forward and permanent delete/empty-trash are **never**
   undoable — say so, don't fake it.
4. **Never hang.** Every `osascript`/JXA call runs as a subprocess with a hard timeout and
   process-group kill on expiry (`write/jxa_executor.py`); the MCP server is async and never blocks
   the event loop on that subprocess; `Envelope Index` reads use `immutable=1` (no lock waits);
   `index.db` uses WAL + `busy_timeout`; broad `whose message id` scans are capped and return
   `Timeout` rather than hang; `--watch` is off the request path and self-healing.
5. **No focus-dependent UI automation.** Never drive Mail.app compose via simulated keystrokes or
   NSPasteboard injection (the fragile upstream HTML-body trick). Build messages via the AppleScript/
   JXA scripting dictionary or a generated `.eml` opened as a draft.
6. **Reads never write Mail's database.** `read/envelope_reader.py` opens `Envelope Index`
   read-only/immutable. All derived state lives in our own `index.db`, which must always be
   rebuildable from disk (`index rebuild`).
7. **Docs-as-you-go.** Any change to a subsystem updates its mapped Wiki page in the same commit
   (see Knowledge map below); `scripts/check_docs_sync.py` enforces this.

## Knowledge map (subsystem → source → Wiki page → key invariant)

| Subsystem | Source | Wiki page | Key invariant |
|---|---|---|---|
| Identity & resolution | `core/identity.py`, `core/resolver.py` | [Identity & resolution](docs/wiki/Identity-and-resolution.md) | #1 above |
| Safety / confirm / undo | `core/safety.py`, `core/undo.py` | [Safety, confirmation & undo](docs/wiki/Safety-confirmation-and-undo.md) | #2, #3 |
| Storage / schema | `storage/database.py`, `storage/migrations.py` | [Apple Mail on-disk format](docs/wiki/Apple-Mail-on-disk-format.md) | #6 |
| Envelope/.emlx readers | `read/envelope_reader.py`, `read/emlx_parser.py` | [Apple Mail on-disk format](docs/wiki/Apple-Mail-on-disk-format.md) | #6 |
| Indexer & watch | `read/indexer.py`, `read/watcher.py` | [Indexing and watch](docs/wiki/Indexing-and-watch.md) | #4 |
| Search (keyword/hybrid) | `read/search.py`, `read/vector_search.py` | [Search](docs/wiki/Search.md) | n/a |
| Threading & knowledge | `read/threader.py`, `knowledge/*` | [Threading and knowledge](docs/wiki/Threading-and-knowledge.md) | n/a |
| Write layer | `write/*` | [Architecture](docs/wiki/Architecture.md) | #4, #5 |
| Tools (MCP surface) | `tools/*` | [Tools reference](docs/wiki/Tools-reference.md) | #1, #2 |
| Resources & recipes | `resources/*`, `skills/*` | [Resources and prompts-recipes](docs/wiki/Resources-and-prompts-recipes.md) | n/a |
| Config & CLI | `config.py`, `cli.py` | [Configuration reference](docs/wiki/Configuration-reference.md) | n/a |
| Packaging | `pyproject.toml`, `scripts/build_pyz.sh` | [Single-file packaging](docs/wiki/Single-file-packaging.md) | see below |
| On-device LLM helper (optional, not built by default) | `read/llm_helper.py`, `swift/foundation-models-summarizer/` | [Search](docs/wiki/Search.md) | #4 |

## Task playbook

- **Add/change a tool** → read *Tools reference* + `tools/<module>.py`; update the tool→module table
  there and in the plan if the backend changes.
- **Change indexing or `--watch`** → read *Indexing and watch* + `read/indexer.py`/`read/watcher.py`;
  re-run `apple-mail-mcp index status` to sanity check staleness reporting.
- **Touch identity/resolution** → read *Identity & resolution* first; this is the correctness-critical
  core — never weaken the read-back verification or the `MultipleMatches` rule.
- **Touch any write tool** → confirm it still goes through `guard()`; check batch caps and undo
  journaling didn't silently drop.
- **Add a recipe** → `skills/<name>/recipe.yaml` + `prompt.md`, then `skills/loader.py` picks it up
  automatically; document it in *Resources and prompts-recipes*.

## Packaging notes (single-file `.pyz`)

All packaged data (JXA/AppleScript templates, recipe YAML, `config.toml.example`) must be loaded via
`importlib.resources`, never a hardcoded `__file__`-relative path — `shiv` zipapps need this to
resolve. Every optional native dependency (`watchfiles`, `sqlite-vec`, `pyobjc`, `onnxruntime`) is
imported lazily/guarded so the core `.pyz` runs without them. The `sqlite-vec` loadable extension is
extracted to a temp path before `load_extension()` — C extensions can't load from inside a zip.
`swift/foundation-models-summarizer/` is a separate Swift package, never bundled into the Python
wheel or `.pyz` — it's built independently (`swift build -c release`) and located at runtime via
`read/llm_helper.py::find_binary()`.

## Conventions

- Python 3.10+, type hints everywhere, pydantic models for all tool I/O.
- No comments explaining *what* code does; only non-obvious *why* (a constraint, a workaround, an
  invariant from the table above).
- Config precedence: CLI > env (`APPLE_MAIL_*`) > `config.toml` > defaults (`config.py`).
- Tests must run without a Mac: synthetic `.emlx` fixtures + a tiny crafted Envelope Index; the
  `write/jxa_executor` boundary is mocked.
