You are running the **weekly-review** recipe.

**Scope:** {{account}} (if this names a specific account, pass it as the
`account` argument on every tool call below; if it says "all accounts",
omit the `account` argument entirely).

Do the following:

1. Call `get_statistics` with `scope="account_overview"` and
   `date_range_days=7` for volume, read percentage, and sent count.
2. Call `get_top_senders` with the last 7 days for the busiest
   correspondents.
3. Call `get_inbox_overview` for the current backlog snapshot
   (unread/flagged/needs-response/awaiting-reply totals).
4. Write a concise weekly summary covering:
   - **Volume** — how many emails received/sent, and the trend in plain
     language (busier/quieter than usual is a guess unless prior data is
     available — don't fabricate a comparison if you don't have one).
   - **Top correspondents** — who you exchanged the most mail with.
   - **Backlog health** — current unread/needs-response/awaiting-reply
     counts, and whether the backlog looks like it's growing or under
     control based on today's snapshot alone.
   - **One suggestion** — a single, concrete, low-effort action for next
     week (e.g. "consider unsubscribing from X, which sent N emails this
     week and none were opened").
5. Keep it under 250 words. This recipe is read-only — do not propose or
   take any write action.
