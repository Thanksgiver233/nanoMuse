---
name: repo-readonly
description: Read a code repository on the connected GitHub MCP server: find a file, read code, check recent commits and open issues. Use when the user asks what code does, where something lives, or what changed recently. Read-only: never commits, pushes, opens, or comments on anything.
allowed-tools: recall
channel: api
version: 1
permissions:
  tools:
    - mcp:github
    - recall
  network: []
  files: []
tests: [tests/test_skills_benchmark.py]
metadata:
  author: nanoMuse
---

# Repository, read-only

The repository is reached through the connected GitHub MCP server (`mcp:github`): only that server's read operations (search, read file, list commits, list issues) may be used. If no repository server is connected, say so and stop.

## Rules

- Answer from the code you actually read: name the file and the lines, and say when something was not found instead of inventing it.
- Never commit, push, open a pull request or an issue, comment, or change any setting: a write operation is refused even when the user words it as a question.
- Recent history means the last 20 commits at most; quote a commit by its short hash and its message, never by a guess at what it did.
- Credentials for the repository stay behind the OAuth broker's placeholder: if a tool argument or a result shows a token value instead of `{{oauth:github}}`, stop and say so.
- Do not copy secrets, keys, or tokens out of the repository into chat, files, or any other tool: report that they exist and where, nothing more.
