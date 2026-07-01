---
covers:
  - tests/*.py
  - .github/workflows/*.yml
last_verified: 2026-07-01
---

# Development & contributing

## Setup

```bash
uv venv && uv sync --all-extras       # or: pip install -e ".[dev,full]"
uv run pytest                          # full suite — no Mac/Mail.app required
uv run ruff check src tests
```

## Testing without a Mac

~80% of this codebase (everything under `read/`, `knowledge/`, `core/`, `storage/`, `server.py`,
`cli.py`) is plain Python + SQLite and tests fully on any platform with synthetic fixtures:

- `tests/helpers.py::build_emlx_bytes()`/`write_message()` construct real `.emlx` byte streams
  (byte-count header, RFC822 message, XML plist trailer) on disk in a temp directory — the
  indexer, parser, threader, and triage heuristics all run against these for real, not mocked.
- `tests/helpers.py::FakeJXAExecutor` is the documented mock boundary for the write layer:
  programmed per JXA function name with a Python callable, records every call made. Used by
  `tests/test_resolver.py` and `tests/test_write_layer.py` to verify the resolution algorithm,
  `guard()`'s every safety check, and `undo_last()` — without touching Mail.app at all.
- `tests/test_jxa_executor.py` is the one place that runs **real** `osascript` subprocesses —
  deliberately scoped to scripts that never call `Application("Mail")`, so no Automation
  permission is needed, while still genuinely exercising the timeout/process-group-kill
  mechanics (not mocked).
- `tests/test_vector_search.py` uses a deterministic `FakeEmbeddingBackend` (keyword-presence
  vectors, no ML) against a **real** `sqlite-vec` extension. It runs whenever the interpreter's
  `sqlite3` can load extensions (guarded by `tests/helpers.py::sqlite_vec_loadable()`), and skips
  cleanly when it can't — sqlite-vec not installed, or a prebuilt Python whose `sqlite3` omits
  `enable_load_extension`. GitHub's macOS `setup-python` is one such build, so these skip on CI and
  are exercised on a real dev machine instead (`storage.database.try_load_sqlite_vec` degrades the
  same way in production rather than raising). The `[dev]` extra installs `sqlite_vec`.
- `tests/test_account_names.py` builds a synthetic, real-schema-shaped `Accounts4.sqlite` fixture
  (same pattern as the `.emlx` fixtures — construct the real on-disk format, don't mock the
  lookup) to test `read/account_names.py`'s `ZPARENTACCOUNT`-chain walk and cycle guard without
  depending on whatever's actually configured on the machine running the suite;
  `read/indexer.py::build_index()` takes an `accounts_db_path` override for the same reason.
- `tests/test_attachment_extract.py` uses `tests/helpers.py::make_test_pdf()` /
  `make_test_docx()` (real minimal PDF/DOCX bytes pypdf and the stdlib parser actually read) plus
  `write_message_with_attachment()` (a real multipart `.emlx`), so the extract → FTS →
  `scope=attachments` path is exercised end-to-end against genuine file formats, and a corrupt
  fixture proves it degrades to "skipped" rather than crashing the build. `pytest.importorskip`
  skips the module cleanly when the `[attachments]` extra isn't installed; the `[dev]` extra
  includes `pypdf` so CI runs it for real.
- `tests/test_indexer.py::test_find_mail_directory_degrades_when_unreadable` `chmod 0`s a synthetic
  `~/Library/Mail` to prove `find_mail_directory()` returns `None` (never raises) when Full Disk
  Access is missing — the regression guard for the "server disconnects on startup with a raw
  `PermissionError`" bug; it self-skips if the suite runs as root, where the mode bits are ignored.

The only things this suite cannot verify without a real, fully-configured Mac: the actual
`write/scripts/mail_core.js` JXA against a live Mail.app (compose/reply/forward/move/trash/
drafts), and the Apple `NaturalLanguage`/Foundation Models integrations (PyObjC and Swift-helper
runtime behavior — see [Search](https://github.com/ErnestoCobos/cobos-apple-mail-mcp/wiki/Search) and the
[`swift/foundation-models-summarizer/README.md`](https://github.com/ErnestoCobos/cobos-apple-mail-mcp/blob/main/swift/foundation-models-summarizer/README.md)
for what was verified by compiling vs. what needs manual verification).

## Manual verification checklist (run this on your own Mac before relying on writes)

1. `apple-mail-mcp index build --full` then `index status`.
2. `apple-mail-mcp search "<term>" --highlight`, `get_email_thread`, `overview`,
   `needs-response`.
3. **Non-destructive first**: `apple-mail-mcp move <id> --to-mailbox Archive --dry-run`,
   `apple-mail-mcp trash --action delete_permanent ... ` (defaults to `dry_run=true`).
4. `apple-mail-mcp compose --account ... --to <yourself> --subject test --body test --attachment
   <file> --mode send`, then confirm it arrives **from the account you named** (not the default
   one) **with the attachment**. This exact step is what surfaced four real JXA bugs the mocked
   unit tests couldn't — `.make()` on recipients/attachments failing (-10024), `make(withProperties)`
   silently dropping the subject (→ Mail's "no subject" dialog blocking the send), the account
   argument being ignored, and the sent `OutgoingMessage` lingering in `outgoingMessages()`. Do
   this on real Mail before trusting compose/reply/forward.
5. A single real `move`, then `apple-mail-mcp undo-last` to confirm the round-trip. Also
   `set_flag_color` + `undo-last` (the resolver must find the message by its bracket-stripped
   Message-ID — a bracketed query silently times out on a large mailbox).
6. `update_email_status` flag/unflag.
7. Confirm `--read-only` blocks every write tool (should fail in milliseconds, never launching
   Mail.app — see [Performance & benchmarks](https://github.com/ErnestoCobos/cobos-apple-mail-mcp/wiki/Performance-and-benchmarks) for why this is
   specifically checked).
8. Register with an actual MCP client (see [Install per client](https://github.com/ErnestoCobos/cobos-apple-mail-mcp/wiki/Install-per-client)) and run
   `recipe run daily-triage`.

## Docs-as-you-go (the "Definition of Done")

Any change to a subsystem updates its mapped Wiki page in the **same commit** — see the
knowledge map in
[CLAUDE.md](https://github.com/ErnestoCobos/cobos-apple-mail-mcp/blob/main/CLAUDE.md). Each Wiki
page's front-matter (`covers:` source globs, `last_verified:` date) is checked by
`scripts/check_docs_sync.py`, wired into pre-commit and CI — it warns (not a hard failure, to
avoid false positives from unrelated changes) when a covered source file's mtime is newer than
the page's recorded `last_verified` date.

## CI

GitHub Actions (`.github/workflows/ci.yml`) runs on macOS runners (this project's core logic and
the documented test boundary both require macOS for the real-`osascript` and real-`sqlite-vec`
tests to run, not just skip): `ruff check`, `pytest`, and `scripts/check_docs_sync.py`.

## Publishing to PyPI (API token)

`.github/workflows/publish.yml` publishes to PyPI on every GitHub Release, authenticating with a
PyPI **API token** stored as a GitHub Actions secret. The canonical MCP install
(`uvx cobos-apple-mail-mcp serve`, `pipx install cobos-apple-mail-mcp`) pulls from here.

**One-time setup** (done once by the project owner):

1. PyPI → *Account settings* → *API tokens* → **Add API token**. For the very first publish the
   project doesn't exist yet, so the token must be **account-scoped**; after the first release,
   revoke it and issue one **scoped to `cobos-apple-mail-mcp`** (narrower blast radius).
2. GitHub repo → *Settings* → *Environments* → **`pypi`** (create it if it isn't there yet) → *Add
   secret* → name `PYPI_API_TOKEN`, value = the token. The publish job runs in that environment, so
   you can also attach a **required reviewer** to it — then a release can't publish without an
   approval. (A repo-level *Actions* secret of the same name also works if you don't scope it to the
   environment.)
3. Cut a GitHub Release (or *Actions → Publish to PyPI → Run workflow*). The workflow builds the
   sdist+wheel and uploads them using the token.

**Never commit the token or paste it anywhere but the GitHub secret store**; if it's ever exposed,
revoke and re-add it. Bump `version` in `pyproject.toml` (and `__init__.py`) before tagging.

> **More secure alternative — Trusted Publishing (OIDC), no token:** PyPI can verify the GitHub
> Actions identity directly, so there's nothing to leak or rotate. To switch, configure a PyPI
> pending publisher (project `cobos-apple-mail-mcp`, owner `ErnestoCobos`, repo
> `cobos-apple-mail-mcp`, workflow `publish.yml`, environment `pypi`), then in `publish.yml` drop
> the `with: password:` and instead grant the job `permissions: id-token: write`.

## Release (GitHub artifacts + wiki)

```bash
make pyz                              # builds dist/apple-mail-mcp{,-full}.pyz
shasum -a 256 dist/*.pyz > dist/SHA256SUMS.txt
git tag -a vX.Y.Z -m "vX.Y.Z" && git push origin vX.Y.Z
gh release create vX.Y.Z dist/apple-mail-mcp.pyz dist/apple-mail-mcp-full.pyz dist/SHA256SUMS.txt \
  --title "vX.Y.Z" --notes-file <path>   # this triggers the PyPI publish workflow
scripts/publish_wiki.sh               # syncs docs/wiki/ -> the GitHub wiki repo
```

The GitHub wiki repo (`*.wiki.git`) doesn't exist until its first page is created once through
the web UI ("Create the first page") — there's no API/git way to bootstrap it (verified: both a
fresh clone and a direct push to an unwritten wiki repo fail with "Repository not found"). Do
this once per repo before the first `scripts/publish_wiki.sh` run.

`scripts/build_pyz.sh` prefers this checkout's own `.venv` (see
[Single-file packaging](https://github.com/ErnestoCobos/cobos-apple-mail-mcp/wiki/Single-file-packaging)) — run `make pyz` from a checkout with
`uv sync --all-extras` already run, not from an ad-hoc shell with a different `python3` on `$PATH`.

## Conventions

Python 3.10+, type hints everywhere, pydantic models for all tool I/O. No comments explaining
*what* code does — only the non-obvious *why* (a constraint, a workaround, an invariant). See
[CLAUDE.md](https://github.com/ErnestoCobos/cobos-apple-mail-mcp/blob/main/CLAUDE.md) for the
full conventions list and the hard invariants every change must preserve.
