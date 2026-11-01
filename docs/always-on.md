# Always-on companion, squads, sync v2, and the cost ledger

This page began as the Phase 4 scaffolding description: simulation
harnesses with fake transports and fixture data, whose shapes were the
contract a real implementation must keep. Phase 6 has since made two
of the four pieces real (the companion's Telegram channel and on-disk
state, and the cost ledger fed from real runs, with a budget gate).
The squad and sync v2 sections still describe scaffolding. Each
section below says plainly which state it is in.

## The companion runtime

`nanomuse/companion/runtime.py` holds a minimal runtime that does
three things: scheduled tasks, IM receive and send, and approval
forwarding. It is off by default. Off means off: starting it returns
false, and every entry point refuses, as if the runtime did not exist.
A caller supplies a channel port with two calls, `send_message` and
`poll_inbound`.

Phase 6 status: the port has one real implementation,
`nanomuse/companion/telegram_port.py`, a synchronous Telegram Bot API
client (sendMessage and getUpdates, update offset persisted so a
restart does not re-deliver), and `nanomuse/companion/store.py` keeps
scheduled tasks and working states on disk, so both survive a restart
and a task that ran before it never runs twice after it.
`nanomuse/companion/factory.py` builds the runtime from the
`[companion]` settings. What is still missing: the runtime is not yet
wired into the running server, there is no deployment or VPS behind
it, and the 8 hour shutdown test has not been run. The Hub transport
between runtimes is still the in-memory fake; only the IM channel and
the local state are real.

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
record per month when spending reaches the threshold.

Phase 6 status: the ledger is now fed from real runs.
`nanomuse/cost/recording.py` turns each finished task's budget
snapshot (the agent loop already meters every call) into a ledger
entry, and `nanomuse/cost/store.py` persists entries as JSONL under
the data directory, so monthly totals survive restarts. Recording is
on when the `[cost]` settings set a monthly budget or at least one
price; with neither, nothing is kept and nothing changes. On top of
the alert, `nanomuse/cost/gate.py` adds the budget gate in three
modes the person chooses: `off` (record only), `warn` (the alert, and
work continues: the old behaviour), and `hard` (once the month's
recorded spend reaches the budget, new tasks are refused before they
start; one approved task at a time can be let past with
`override_once`, which the approval flows call).

`scripts/reconcile_costs.py` compares ledger rows against a JSON bill
fixture under one price table and reports the relative error per
model and in total. It exits 0 within tolerance (5 percent by
default), 1 beyond it, and 2 on a usage error. It reconciles
fixtures only. No real vendor bill has been reconciled against this
ledger yet: the entries are real usage now, but the prices are the
person's own table, not a vendor invoice.

## What is still missing

An 8 hour shutdown test with a real deployment, wiring of the
companion runtime into the running service, and a real bill
reconciliation. The IM vendor behind the channel port and the ledger
entries are real now (Phase 6); their remaining Gate items are the
deployment, the long test, and the vendor bill, and the test numbers
for those are still fixture numbers, not measured capability.
