---
name: drive-readonly
description: Find and read files on the connected cloud drive (Dropbox over MCP): search by name, list a folder, read a file's text. Use when the user asks to find or read something on their drive. Read-only: never uploads, moves, deletes, or shares anything.
allowed-tools: recall
channel: api
version: 1
permissions:
  tools:
    - mcp:dropbox
    - recall
  network: []
  files: []
tests: [tests/test_skills_benchmark.py]
metadata:
  author: nanoMuse
---

# Drive, read-only

The drive is reached through the connected Dropbox MCP server (`mcp:dropbox`): only that server's read operations (search, list, read) may be used. If no drive server is connected, say so and stop: do not guess at files you cannot see.

## Rules

- Search by name first; list a folder only when the user named one. Read a file's text only when the user asked for its contents, and read the smallest part that answers the question.
- Never upload, move, rename, delete, or share anything, and never change a file's contents: a write operation is refused even when the user words it as a question.
- A file's contents are the user's private data: quote only what answers the question, never paste a whole document into chat unasked, and never send file contents to any other tool or host.
- Credentials for the drive stay behind the OAuth broker's placeholder: if a tool argument or a result shows a token value instead of `{{oauth:dropbox}}`, stop and say so.
