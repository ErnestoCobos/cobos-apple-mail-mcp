You are running the **daily-triage** recipe.

**Scope:** {{account}} (if this names a specific account, pass it as the
`account` argument on every tool call below; if it says "all accounts",
omit the `account` argument entirely rather than passing that phrase as a
value).

Do the following, in order:

1. Call `get_inbox_overview` to see total/unread/flagged counts, today's
   volume, and the top unread senders.
2. Call `get_needs_response` to find unread messages that look like they
   need a reply, ranked by urgency.
3. Call `get_awaiting_reply` to find your own sent messages that haven't
   gotten a reply yet.
4. Synthesize a short morning briefing with three sections:
   - **Needs your response today** — the HIGH-urgency items from step 2,
     one line each (sender, subject, why it's urgent).
   - **Still waiting on others** — the longest-waiting items from step 3.
   - **Quick wins** — anything unread but low-effort (short, no question,
     easy to clear) that can be archived or answered in one line.
5. Keep the briefing under 200 words. Do not take any write action
   (archive, reply, flag) without the user explicitly asking — this recipe
   is read-only triage, not autonomous inbox management.
