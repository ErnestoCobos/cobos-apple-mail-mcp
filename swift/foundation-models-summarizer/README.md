# foundation-models-summarizer

Optional, **not built by default**. A small Swift CLI wrapping Apple
Intelligence's Foundation Models framework, for on-device email
summarization — something the JXA write layer can't do, and something
this project's core (Python) deliberately doesn't depend on.

## Why this is a separate Swift binary, not Python

`FoundationModels` is a **Swift-only framework** — it has no Objective-C
bridge, so PyObjC (which `cobos_apple_mail_mcp`'s optional semantic search
layer uses for Apple's `NaturalLanguage` framework) cannot reach it. The
only way to call it from Python is to shell out to a compiled Swift binary.
This is consistent with the project's "every external call is a bounded
subprocess" rule (see `write/jxa_executor.py`).

## Requirements (verified on this machine, 2026)

- macOS **26** or later, Apple Silicon — `LanguageModelSession` and
  `respond(to:)` are gated to macOS 26.0 in the current SDK (Xcode 26.x).
  An earlier `@available(macOS 15.0, *)` annotation will fail to compile —
  this is real, not a guess; see the build log this scaffold was verified
  against.
- Apple Intelligence enabled in System Settings, with the on-device model
  downloaded.
- Swift 6.2+ toolchain (`swift-tools-version: 6.2` is required for the
  `.macOS(.v26)` platform constant in `Package.swift`).

## Build

```bash
cd swift/foundation-models-summarizer
swift build -c release
# binary at .build/release/foundation-models-summarizer
```

The Python integration point (`src/cobos_apple_mail_mcp/read/llm_helper.py`)
looks for the binary in this order: `$APPLE_MAIL_LLM_HELPER` env var, `$PATH`,
then this directory's `.build/release/` (for local development). No tool in
the server calls into this yet — wiring up an actual `summarize_thread` MCP
tool on top of `read/llm_helper.py::summarize()` is deferred until
requested; the calling convention below is what such a tool would use.

## Protocol

One JSON object on stdin, one JSON object on stdout, process exits — no
daemon, no persistent state.

```bash
echo '{"task": "summarize", "text": "Hi Bob, can we meet Friday at 3pm?"}' \
  | .build/release/foundation-models-summarizer
```

```json
{"summary": "Alice is asking Bob to meet on Friday at 3pm."}
```

or, on any failure (model unavailable, guardrail refusal, unsupported OS):

```json
{"error": "..."}
```

## Known limitation: a bare CLI binary may hang on first run

`Package.swift` and `main.swift` here compile cleanly under Swift 6.2 /
macOS 26 (verified on this machine — `swift build` succeeds). Actually
*running* the resulting binary against a real `LanguageModelSession`,
however, did not return within a reasonable time on this machine and had to
be killed; a plain `swift build` executable likely lacks the code-signing/
entitlement context (and possibly the on-device model isn't downloaded)
that `Application("Mail")`-style sandboxed/entitled apps get for free. If
you build and use this for real:

- Confirm Apple Intelligence is fully enabled and the on-device model is
  downloaded in System Settings before testing.
- If the bare binary still hangs, the likely fix is packaging it as a
  proper signed `.app` bundle (or running it under a host process that
  already has the entitlement) rather than a loose command-line tool —
  this needs verification on a real, fully-configured Mac, which is
  exactly the kind of check this project's own testing philosophy reserves
  for the user (see CLAUDE.md). Treat this scaffold as compile-verified,
  not run-verified.

## Known limitation: ~4K token window

Foundation Models' on-device session has a combined input+output context of
roughly 4,096 tokens (a Private Cloud Compute variant offers more, but that
leaves the device — out of scope for a local-first tool). Keep `text` to a
single conversation thread, not a whole mailbox.
