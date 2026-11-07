# Cloud computers

With `[cloud_pc]` on, the agent can open a computer on a cloud provider, work
in it and destroy it: six tools, `cloud_create`, `cloud_status`, `cloud_exec`,
`cloud_file`, `cloud_screenshot` and `cloud_destroy`. The first provider is
Daytona, whose sandboxes are isolated Linux machines with their own kernel
and filesystem. The point is the shape, not the vendor: a machine the agent
can open on purpose, whose cost is recorded, and whose opening the budget
gate and the Sentinel both see.

## What a run looks like

`cloud_create` opens a machine and returns its id. `cloud_exec` runs shell
commands in it, `cloud_file` moves one file each way between the workspace
and the machine, `cloud_screenshot` saves a picture of its desktop into the
workspace, and `cloud_destroy` deletes the machine and stops the billing.
`cloud_status` lists what this device has opened, the provider's current
state for the live ones, and the month's recorded spend.

Machines auto-stop after `[cloud_pc] idle_timeout_minutes` idle (30 by
default). Auto-stop is the provider's pause, not the end of the record: a
machine counts as open, and against `max_machines`, until `cloud_destroy`.

## How the Sentinel sees them

- `cloud_exec` is shell on a remote machine: sensitive, egress, the same
  posture as the local `shell` tool.
- `cloud_create` is moderate and carries a warning that it starts a
  billable machine.
- `cloud_destroy` is sensitive with a warning, because it is irreversible:
  the machine and its disk go together.
- `cloud_file` up is moderate with egress; down is moderate and reads
  private data, because the file's content comes back into the workspace.
- `cloud_screenshot` is moderate and reads private data: a screen can hold
  anything the machine was showing.

The standing-grant key for a call is the tool's own name, as with every
tool, so a person can settle `cloud_exec` calls in advance without settling
`cloud_destroy`.

## What it costs, and the gate

Every machine is recorded in `cloud_pc.json` in the data directory: size,
create and destroy times, and one spend entry written at destroy. The
recorded cost is wall-clock time from create to destroy at
`hourly_rate_per_vcpu_usd` per vCPU (0.0666 by default, the effective rate
measured on the account this was built against; it is a configuration
value, not a provider quote). A machine the provider auto-stopped may have
billed less than the record says. Reconciling this record against the
provider's bill, the way the model ledger was reconciled in Phase 6, is
planned work; until then the record is the number to reconcile, and the
docs and the tool both say so.

The budget gate reads the same record, in the `[cost]` gate's three modes:
`off` records only, `warn` (the default) adds a warning line to the create
result once the month's recorded spend reaches `monthly_budget_usd`, and
`hard` refuses to open a new machine at all. Because spend is finalised at
destroy, a hard gate can still be crossed by the machine already running;
`max_machines` (1 by default) is the bound that keeps that gap small.

## The key

`api_key` is the provider's key, written as a literal or as
`{{vault:NAME}}`; it is resolved from the vault when the app starts, held
by the client, and never shown to the model or written to the record. With
the section enabled and no key resolvable, the tools exist and every call
fails with that fact, so a missing key is loud rather than silent.

## What is not here yet

- One provider. A second provider behind the same record and gate is the
  A2 step, and reconciling the spend record against a provider bill is
  part of it.
- No idle sweeper: nothing yet destroys a machine the agent forgot. The
  companion-side sweep is the A4 step; until then the auto-stop interval
  and `cloud_status` are the safety net, and both are named here so the
  gap is not a surprise.
- Screenshots need a desktop in the machine. A sandbox from a plain image
  has none, and the tool returns the provider's error rather than a
  picture of nothing.
