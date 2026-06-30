You are running the **awaiting-reply** recipe.

**Scope:** {{account}} (if this names a specific account, pass it as the
`account` argument on every tool call below; if it says "all accounts",
omit the `account` argument entirely). **Window:** look back {{days_back}}
days (pass this as `days_back`).

Do the following:

1. Call `get_awaiting_reply` with the scope and window above to list your
   sent messages that haven't received a reply.
2. For each item, sorted by longest-waiting first, write one line:
   recipient, subject, days waiting, and a one-sentence guess at why a
   nudge might help (e.g. "likely buried — short original message").
3. For the top 3 longest-waiting items, optionally call `get_email_thread`
   to check the full context, then draft (do not send) a short, polite
   follow-up message for each — 2-3 sentences, referencing the original
   ask without re-explaining it at length.
4. Present the drafts to the user for review. **Do not call
   `reply_to_email` or `compose_email` to actually send anything** — this
   recipe only proposes follow-ups; sending is a separate, explicit user
   decision.
