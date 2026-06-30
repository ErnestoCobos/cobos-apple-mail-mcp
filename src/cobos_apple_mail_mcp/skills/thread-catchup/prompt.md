You are running the **thread-catchup** recipe.

**Target:** message_id="{{message_id}}", thread_id="{{thread_id}}" (use
whichever one is non-empty as the corresponding argument to
`get_email_thread`; if both are empty, ask the user which message or
thread they mean before doing anything else).

Do the following:

1. Call `get_email_thread` with the target above.
2. Walk the returned tree in chronological order (oldest first) and write
   a catch-up summary covering:
   - **Who's involved** — the participants.
   - **What's been decided** — any clear decisions or commitments made so
     far.
   - **Open questions** — anything asked but not yet answered in the
     thread.
   - **Where it stands now** — whether the last message is waiting on the
     user (their account appears as the most recent sender's *recipient*)
     or waiting on someone else.
3. Keep the summary under 200 words regardless of how long the thread is
   — link back to specific messages by subject/sender rather than quoting
   long passages.
4. This recipe is read-only — do not propose or take any write action
   unless the user asks a follow-up question that requires one.
