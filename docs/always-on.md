# Always-on companion, squads, sync v2, and the cost ledger (mock scope)

This page describes the Phase 4 scaffolding. Every piece here is a
simulation harness with fake transports and fixture data. Nothing on
this page runs on a real server, talks to a real IM vendor, or reads a
real vendor bill. The shapes are the contract a real implementation
must keep; the behaviour behind them is still to be built.

## The companion runtime

`nanomuse/companion/runtime.py` holds a minimal runtime that does
three things: scheduled tasks, IM receive and send, and approval
forwarding. It is off by default. Off means off: starting it returns
false, and every entry point refuses, as if the runtime did not exist.
There is no deployment attached to it, no VPS is bought or configured,
and no real channel is wired in. A caller supplies a channel port with
two calls, `send_message` and `poll_inbound`.

When a device goes offline mid-task, the task's state travels as a
`working` frame: task id, conversation id, the next step to run, the
payload, and the steps already finished. The fake transport queues
frames while the link is down and delivers them in order when it
comes back, so a resumed task never repeats a finished step and never
loses one. The companion never decides an approval. It frames the
card, forwards it to the owner's device, and the decision comes back
as a separate frame.

## The squad

`nanomuse/team/squad.py` holds the optional squad mode: a coordinator
plus research, execute, and verify specialists, talking over frames
shaped like Hub frames. The default stays a single steward; a squad
only exists when the caller forms one.

Approvals belong to the steward alone. A specialist that requests an
approval, confirms one, hands a token to another specialist, or runs a
sensitive action on a forged, reused, or wrong-target token is
refused, and the refusal is written to the squad audit log. A
sensitive action runs only on a live, single-use token the steward
issued for that specialist, action, and target.

## Sync v2

`nanomuse/sync_v2.py` holds versioned records and tiered recording
consent. It lives outside the `nanomuse.sync` package because that
package's initialisation pulls in the server, which pulls the sync
package back; sync v2 has no server dependency and stays importable
on its own.

Each record carries a version and the version it was built on. A
remote record that builds on the local version applies. A stale
remote is a no-op. Both sides editing from the same base version, or
one side deleting what the other side changed, produces an explicit
conflict object that holds both versions; the local record is left
untouched until the conflict is resolved by an explicit choice. No
sync step silently drops data.

Recording consent is three switches: sync text, full transcript, and
training view. All three default to off, each switches on its own,
and outside input can never flip one: an inbound record that claims a
consent change is ignored and counted. The existing record defaults
elsewhere in the codebase are untouched.

## The cost ledger

`nanomuse/cost/ledger.py` records one entry per model call: task,
device, model, tokens in and out, and the cost from a
per-million-token price table. Three views sum the same entries by
task, by device, and by model. A monthly budget raises one alert
record per month when spending reaches the threshold; the alert is a
record, not a block, and work is never refused for cost here.

`scripts/reconcile_costs.py` compares ledger rows against a JSON bill
fixture under one price table and reports the relative error per
model and in total. It exits 0 within tolerance (5 percent by
default), 1 beyond it, and 2 on a usage error. It reconciles
fixtures only. No real vendor bill has been reconciled against this
ledger.

## What is still missing

An 8 hour shutdown test with a real deployment, a real IM vendor
behind the channel port, a real bill reconciliation, and any wiring
of these modules into the running service. Until those exist, the
Gate 4 items that depend on them stay unmet, and the numbers in the
tests are fixture numbers, not measured capability.
