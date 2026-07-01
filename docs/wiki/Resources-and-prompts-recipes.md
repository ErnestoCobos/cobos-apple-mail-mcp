---
covers:
  - src/cobos_apple_mail_mcp/resources/email_resources.py
  - src/cobos_apple_mail_mcp/skills/*
last_verified: 2026-06-30
---

# Resources and prompts/recipes

## Resources (`email://...`)

`resources/email_resources.py` registers read-only projections of the same functions backing the
tools — a single source of truth, never a parallel implementation.

| URI | Returns |
|---|---|
| `email://accounts` | `list[Account]` |
| `email://mailboxes/{account}` | `list[Mailbox]` |
| `email://threads/{thread_id}` | `EmailThread` (JWZ-reconstructed) |
| `email://message/{message_id}` | `EmailFull` |
| `email://contacts` | `list[ContactSummary]` (browsable, bidirectional; top 100 by volume) |
| `email://contacts/{address}` | `Contact` |
| `email://inbox-summary` | `InboxOverview` |
| `email://awaiting-reply` | `list[AwaitingReplyItem]` |
| `email://needs-response` | `list[NeedsResponseItem]` |
| `email://stats` | `Statistics` (default scope/window) |

All resource functions return a single JSON string (`_dump_json()`), even for list-shaped
results — a list-returning resource function (`-> list[dict]`) confuses FastMCP's content
normalization, which treats a bare returned list as "a list of resource contents" rather than
"one content blob containing a list." This was found and fixed during development; every
resource handler here explicitly serializes to one JSON string.

## Recipes (packaged MCP prompts)

The Spark-CLI-style "skill/recipe" model, implemented **natively as MCP prompts**
(`skills/loader.py`) rather than a parallel invocation channel — any MCP client gets them
automatically via the standard `prompts/list` and `prompts/get` (or `render_prompt` in FastMCP's
own API) calls.

### Format

```
skills/<name>/
├── recipe.yaml
└── prompt.md
```

```yaml
name: daily-triage
description: Triage today's inbox and propose a concrete action plan.
arguments:
  - name: account
    required: false
    description: Restrict triage to one account (omit for all accounts).
    default: "all accounts"
uses_tools: [get_inbox_overview, get_needs_response, get_awaiting_reply]
uses_resources: [email://inbox-summary, email://needs-response, email://awaiting-reply]
prompt_template: prompt.md
```

`prompt.md` is the prompt text, with `{{argument_name}}` placeholders substituted at render time
(`skills/loader.py::render_recipe()`).

### How a recipe becomes an MCP prompt

`skills/loader.py::register_prompts()` discovers every `recipe.yaml`/`prompt.md` pair and
dynamically builds a Python function whose **signature matches the recipe's declared
arguments**, via `inspect.Signature` + an explicit `__annotations__` override (FastMCP's schema
generation resolves parameter types through `typing.get_type_hints()`, which reads
`__annotations__` directly — `__signature__` alone is not sufficient; this was discovered by
attempting it and getting a `KeyError` from Pydantic's schema generator, then fixed). Argument
names always come from the project's own packaged `recipe.yaml` files, never runtime input, so
this dynamic-signature approach carries no injection risk.

### Bundled recipes

| Name | Purpose | Arguments |
|---|---|---|
| `daily-triage` | Morning briefing: needs-response, awaiting-reply, quick wins | `account?` |
| `inbox-zero` | Propose respond/archive/defer for every unread message; **never writes without explicit confirmation** | `account?` |
| `awaiting-reply` | Surface unanswered sent messages and draft (not send) follow-ups | `account?, days_back?` |
| `weekly-review` | Volume, top correspondents, backlog health, one suggestion | `account?` |
| `thread-catchup` | Summarize a conversation thread | `message_id?, thread_id?` |

Each prompt explicitly instructs the model on which tool calls to make, what to never do without
confirmation, and a length budget — see the `prompt.md` files themselves for the exact wording.

### Running a recipe

- **MCP client**: invoke the prompt named `daily-triage` (etc.) with its arguments — works in any
  MCP client that supports prompts.
- **CLI**: `apple-mail-mcp recipe list` / `apple-mail-mcp recipe run daily-triage --arg
  account=Work` (repeatable `--arg KEY=VALUE`; prints the rendered prompt text — running a recipe
  via the CLI does **not** execute any tool calls itself, it just renders the prompt for you or
  an agent to act on).

### Authoring a new recipe

Add a new `skills/<name>/` directory with `recipe.yaml` + `prompt.md` — `register_prompts()`
picks it up automatically on next server start, no code changes needed. Keep argument types to
`str` (the loader's dynamic-signature builder is `str`/`str | None` only, sufficient for every
bundled recipe so far).
