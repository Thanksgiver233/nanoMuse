# Threat model

This page is the structured version of the summary in
[Sentinel](sentinel.md#what-this-does-and-does-not-protect-against): what
nanoMuse holds, who it is defended against, where the boundaries are, and
which threats each mechanism answers. It describes the system as it is
built today. Where a defence is partial, the remainder is named rather
than smoothed over.

## What is being protected

- **Credentials.** Mail, calendar and model keys, and anything else kept
  in the vault, plus the tokens of the app itself.
- **The person's data.** Files in and around the workspace, mail,
  contacts, memories and conversation history.
- **The machine.** nanoMuse runs as the person's own user on their own
  computer, so a tool call that escapes its gate can do anything that
  user can do.
- **The record.** The audit log is the account of what the agent did and
  what was approved. Decisions made later (revoking a grant, disputing
  an action) are only as good as this record.
- **Standing authority.** Approvals that persist (grants, scheduled
  tasks, saved skills) act later, unattended. Their integrity matters as
  much as any single call's.

## Who it is defended against

- **The model itself.** It may be mistaken, manipulated or simply
  over-eager. It is treated as untrusted: it proposes calls, the
  Sentinel disposes.
- **Content the model reads.** Web pages, mail, files and tool results
  can carry instructions written by a third party (prompt injection).
  Content is data, but a model may obey it, so the defence has to hold
  even when it does.
- **A tool or MCP server.** Built-in or third-party, it runs with the
  runtime's privileges once a call is approved.
- **Another user or process on the same machine.** Not the owner: a
  second account, or unrelated software running as one.
- **A network neighbour.** Someone who can reach the app's API port, or
  sit between the app and a service it calls.
- **A forged or replayed message.** On the relay and on the companion
  channel, a repeated delivery must not become a repeated action.

Not defended against: the owner themselves, an attacker who already
controls the owner's OS account or the kernel, and the model provider,
which necessarily sees what is sent to it. Those are stated as
assumptions below, not oversights.

## Trust boundaries

1. **Model to Sentinel.** Every tool call crosses here. Nothing executes
   on the model's say-so.
2. **Sentinel to tools and subprocesses.** Approved calls run as the
   owner. On Linux the sandbox narrows what a subprocess can see and
   reach; elsewhere the narrowing is the scrubbed environment and the
   working directory only.
3. **App to gatekeeper.** When enabled, a separate process holds the
   audit signing key and a second opinion on every call. The channel
   names its peer by OS credential (user id on Linux and macOS, SID on
   Windows); the key never crosses it.
4. **App to vault.** Secrets enter config and connectors as
   `{{vault:NAME}}` placeholders. The model sees the placeholder; the
   value is substituted after approval, at execution.
5. **App to relay and devices (Hub).** Devices of one account talk
   through the relay. A call and its answer cross the network here.
6. **App to the outside world.** Egress from tools: fetches, mail,
   messages. This is the boundary the taint rule watches.

## Threats and what answers them

| Threat | What answers it | What remains |
|---|---|---|
| Injected content talks the model into sending private data out | Taint tracking: once a conversation has read private data, egress to a host outside the allowlist asks first, with the destination on the card, in every mode including `auto`. `web_fetch` also refuses private, loopback and metadata addresses on every hop | Data that leaves through an approved destination, or through a channel that is not a tool call (embeddings), is outside the rule; both are named in the Sentinel page |
| The model runs a call the person never meant to allow | Risk levels, per-call assessment, warnings that always ask, approvals bound to a grant key (tool plus target), deny rules that no grant overrides | A person who approves without reading defeats this; the design makes cards specific so that reading them is possible, and cannot do more |
| A secret leaks to the model or into a transcript | The vault: placeholders in, substitution after approval, redaction of known secrets from results, credential-looking variables scrubbed from subprocess environments | A secret the person pastes into chat is conversation content, not a vault value, and travels like it |
| A subprocess escapes the workspace or reads the home directory | On Linux, bubblewrap: only the workspace is writable, home does not exist inside, no network unless the call was assessed as needing it | No sandbox on macOS or Windows, or in most containers: an approved command there runs as the owner. A kernel flaw defeats the namespace anywhere |
| The audit log is edited after the fact to hide an action | The HMAC hash chain: editing, deleting or reordering a line breaks verification (`nanomuse audit --verify`) | Anyone holding both the log and its key can re-sign a whole chain. The chain is tamper-evident, not unforgeable |
| The audit key is taken from the app, so a forged chain passes | The gatekeeper: its key lives in a process of its own, arrives by descriptor at spawn or is read by that process from the OS keychain, has no file fallback, and its decisions are signed into a separate chain | The gatekeeper is opt-in and off by default. It also runs as the same OS user: it separates the key from the agent process, not from the account |
| Another local user asks the gatekeeper for a decision, or reads its channel | Peer checks on every connection: user id from the socket credentials on Linux and macOS, the process token's SID against an owner-only access list on the Windows pipe. A peer from another user gets no answer | A process running as the owner can use the channel; that is the same-user limit above |
| A dropped Hub call is re-sent and the action runs twice | The relay keeps finished results briefly and replays the kept answer to a re-send marked as a retry; a plain re-send is a new call, and another device reusing the id gets a fresh execution | An answer that never reached the relay cannot be replayed, so a caller cannot always tell whether the first attempt ran; the protocol says so rather than guessing |
| A companion message or approval is forged or delivered twice | The companion never decides an approval: it frames the card and forwards it, and the decision returns as its own frame. Its store persists the update offset and task state, so a restart neither re-delivers a message nor re-runs a finished task | The channel port's real implementation is Telegram; its account security is the person's Telegram account security |
| A saved skill or a standing task becomes a quiet way in | Writing a skill is `sensitive` and always asks, as is creating standing work in `strict` mode; grants are listed with the avatar and revocable, and persisted approvals carry expiries where the scope has one | A skill the person approved is trusted thereafter, like any standing instruction they wrote themselves |
| The API port is reached by someone who is not the owner | A bearer token on the API, and the operator guidance in [SECURITY.md](../SECURITY.md): do not expose the port, put TLS in front if it must travel | Plain HTTP on a trusted network is the default shape; exposing it anyway is an operator decision the docs argue against |

## Assumptions

- The owner's OS account is honest. Defences that separate things
  inside the account (the gatekeeper above all) raise the cost of
  abusing the agent process; they do not claim to withstand the account
  itself being hostile.
- The person reads approval cards, at least the ones carrying warnings.
  Several entries above end there, deliberately.
- The model provider sees the conversation it is given, memories
  included. Keeping a class of data out of the model entirely is a
  configuration choice (a local model, a local embedding endpoint), not
  something the Sentinel can enforce after the fact.
- Platform peer credentials and keychains behave as documented by the
  OS. The gatekeeper's guarantees inherit theirs.

## Keeping this page honest

A mechanism named here must exist in the code, and a limit named in the
Sentinel page's summary must not be contradicted here. When a defence
changes, this page changes in the same commit; when a claim here is
found wrong, that is a security report, per
[SECURITY.md](../SECURITY.md).
