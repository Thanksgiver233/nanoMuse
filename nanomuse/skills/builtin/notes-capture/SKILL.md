---
name: notes-capture
description: Capture a note the user dictates or drops in chat: file it under the right heading in the workspace notes, and remember the fact behind it. Use when the user says "note this", "write this down", or drops a thought to keep. Writes only inside the notes folder, never elsewhere.
allowed-tools: files remember recall
channel: api
version: 1
permissions:
  tools:
    - files
    - remember
    - recall
  network: []
  files:
    - notes
tests: [tests/test_skills_benchmark.py]
metadata:
  author: nanoMuse
---

# Notes capture

## File it

- Notes live under `notes/` in the workspace, one file per topic (`notes/<topic>.md`). Read the file first when it exists; append under today's heading, do not rewrite what is already there.
- A note the user dictated word for word goes in as a quote; a thought goes in as one short paragraph in the user's words, not a summary of a summary.
- If no topic fits, ask which one, or file it under `notes/inbox.md` and say so.

## Remember it

- `remember` the fact behind the note (who, what, when), with the note's path as the source, so it can be recalled later without opening the file.
- `recall` the topic first when the note amends something already known: an explicit correction replaces the old line, it does not sit beside it.

Never write outside `notes/`, never touch another file, and never send a note anywhere: capture is local.
