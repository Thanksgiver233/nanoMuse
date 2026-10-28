---
name: email-digest
description: Summarise the recent mail into a short digest: what needs a reply, what is waiting, and what can wait. Use when the user asks for a mail summary, inbox digest, or "what did I miss in email". Read-only: never sends, deletes, or archives anything.
allowed-tools: read_emails recall
channel: api
version: 1
permissions:
  tools:
    - read_emails
    - recall
  network: []
  files: []
tests: [tests/test_skills_benchmark.py]
metadata:
  author: nanoMuse
---

# Email digest

Read-only. If the mail connector (`read_emails`) is not available, say so and stop: do not guess at mail you cannot see.

## Read

- `read_emails` with `unread_only=true` first (up to 50). If there are fewer than 10, read the last 3 days with `unread_only=false` too.
- `recall` "email" for the user's rules: senders that always matter, newsletters they keep, how they like summaries to sound.

## Write

One digest, in this order:

1. **Needs a reply**: someone asked the user something, or is waiting. One line each: who, what they want, since when.
2. **Waiting on them**: the user asked, no answer yet. One line each, with how long.
3. **Can wait**: newsletters, notifications, receipts. Count them, do not list them all.

Never quote a whole mail; a line per thread is enough. Anything that mentions money, a deadline, a contract or health goes to the top of its group. Never send, delete, archive or mark anything: this skill only reads, and says what it found.
