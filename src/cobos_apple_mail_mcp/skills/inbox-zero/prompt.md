You are running the **inbox-zero** recipe.

**Scope:** {{account}} (if this names a specific account, pass it as the
`account` argument on every tool call below; if it says "all accounts",
omit the `account` argument entirely rather than passing that phrase as a
value).

Do the following:

1. Call `get_emails` with `filter="unread"` to list every unread inbox
   message.
2. Call `get_needs_response` to identify which of those genuinely need a
   reply versus which are low-value (newsletters, notifications, FYIs).
3. For each unread message, propose exactly one action: **respond**
   (needs a reply — surface it for the user), **archive** (no action
   needed — propose `move_email` to Archive), or **defer** (flag it for
   later — propose `update_email_status` with `flag`).
4. Present the plan as a table (subject, sender, proposed action, why) and
   ask the user to confirm before executing anything.
5. **Never call a write tool in this recipe without explicit user
   confirmation of the specific batch.** When the user confirms, prefer
   `dry_run=true` first to show exactly what would change, then execute.
   Respect the configured batch limits — if a confirmed batch exceeds the
   limit, split it into multiple confirmed calls rather than trying to
   raise the limit unprompted.
