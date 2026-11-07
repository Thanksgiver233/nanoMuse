# Cloud computers

With `[cloud_pc]` on, the agent can open a computer on a cloud provider, work
in it and destroy it: seven tools, `cloud_create`, `cloud_status`, `cloud_exec`,
`cloud_file`, `cloud_screenshot`, `cloud_desktop` and `cloud_destroy`. Two providers sit
behind one protocol, picked by `[cloud_pc] provider`: Daytona, whose
sandboxes are isolated Linux machines with their own kernel and filesystem,
and InstaBox, whose machines are Firecracker microVMs with a persistent
disk. The point is the shape, not the vendor: a machine the agent can open
on purpose, whose cost is recorded, and whose opening the budget gate and
the Sentinel both see. The tools, the record and the gate never learn which
provider they are talking to; each provider's key and rate live in its own
`[cloud_pc.<provider>]` table.

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
recorded cost is wall-clock time from create to destroy at the provider's
`hourly_rate_per_vcpu_usd` per vCPU (Daytona 0.0666 by default, the
effective rate measured on the account this was built against; InstaBox
0.0085, derived from its published per-second prices for the free plan's
4 vCPU / 8 GiB machine; both are configuration values, not provider
quotes). A machine the provider auto-stopped may have billed less than the
record says.

Reconciliation against the provider's own metering differs by provider.
InstaBox reports each machine's metered usage (`vcpuSeconds`,
`memGiBSeconds`, `runningSeconds` at `GET /v1/machines/{id}/usage`), so
its record can be checked figure for figure. Daytona's API exposes only a
quota-style usage overview for the organisation, no per-machine metered
cost, so on Daytona the record stays the number to reconcile and the
account balance remains a dashboard figure; the docs say so rather than
implying a check the API cannot support.

The budget gate reads the same record, in the `[cost]` gate's three modes:
`off` records only, `warn` (the default) adds a warning line to the create
result once the month's recorded spend reaches `monthly_budget_usd`, and
`hard` refuses to open a new machine at all. Because spend is finalised at
destroy, a hard gate can still be crossed by the machine already running;
`max_machines` (1 by default) is the bound that keeps that gap small.

## The key

Each provider's `api_key` is written as a literal or as `{{vault:NAME}}`;
it is resolved from the vault when the app starts, held by the client, and
never shown to the model or written to the record. With the section
enabled and no key resolvable for the selected provider, the tools exist
and every call fails with that fact, so a missing key is loud rather than
silent.

Provider shapes worth knowing: an InstaBox create sends no size (the plan
fixes it) and records what the machine reports, its create and delete are
operations to follow rather than single answers, and `cloud_exec` first
wakes a machine the provider suspended for idleness. An InstaBox delete
echoes the machine's own id in a confirm header, which the client always
sends.

## The desktop (InstaBox)

An InstaBox machine from the `agent-browser` template has a Linux desktop,
and `cloud_desktop` drives it: `status` and `start` for the desktop itself,
`screenshot`, `click`, `move`, `drag`, `scroll`, `type`, `key` and
`open_url`. Coordinates are pixels in the screenshot, which is the desktop
at its started size (1280x800 by default). A verb starts the desktop first
when it is not running, and `start` on a template without one installs it
first, which takes about half a minute.

The mechanism follows the provider's own SDK rather than its VNC stream:
every verb is a command inside the machine over the same exec channel as
`cloud_exec` (`xdotool` for input on `DISPLAY=:1`, `scrot` for the
picture), and a screenshot is that picture downloaded as a file. Typed
text travels as the exec call's stdin, never inside a command string.
There is no new protocol and no new dependency; the price is one exec
round trip per verb, which suits task-level driving and not continuous
control. On Daytona the input verbs answer that desktop input is an
InstaBox capability; screenshots there keep working as before.

The Sentinel posture is per action, as with the local computer tool:
`status` is safe, `start` moderate, a screenshot moderate and reading
private data, and the verbs that move, click or type are sensitive, with
typing also marked egress.

## What is not here yet

- No idle sweeper: nothing yet destroys a machine the agent forgot. The
  companion-side sweep is the A4 step; until then the auto-stop interval
  and `cloud_status` are the safety net, and both are named here so the
  gap is not a surprise.
- Screenshots need a desktop in the machine. A Daytona sandbox from a
  plain image has none, and the tool returns the provider's error rather
  than a picture of nothing. An InstaBox machine gets its desktop through
  `cloud_desktop` above.
