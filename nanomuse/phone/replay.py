"""Replay a recorded Hands trace against a fresh run (Phase 2).

A trace is a JSON Lines file of ``task`` / ``step`` / ``degrade`` / ``end``
records (see :mod:`nanomuse.phone.trace`). Replay feeds each recorded step
to a ``runner`` — a dry-run device, a scripted stand-in, or the operator
itself on a fixture — and compares what comes back with what was recorded:
the action taken and the hash of the screenshot the step saw. A step
matches when both agree; the report says how many did, and lists the ones
that did not, so a failure can be re-run one step at a time instead of
from the top.

The runner answers each step record with a dict holding ``action`` (the
action sentence, as recorded) and ``screen_sha256`` (the hash of the
screenshot it was shown, or None when it saw none).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

StepRunner = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass
class StepReplay:
    step_no: int
    recorded_action: str | None
    replayed_action: str | None
    recorded_sha256: str | None
    replayed_sha256: str | None
    matched: bool
    mismatch: str = ""


@dataclass
class ReplayReport:
    trace_id: str
    steps: list[StepReplay] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.steps)

    @property
    def matched(self) -> int:
        return sum(1 for step in self.steps if step.matched)

    @property
    def consistency(self) -> float:
        return self.matched / self.total if self.total else 1.0

    @property
    def mismatches(self) -> list[StepReplay]:
        return [step for step in self.steps if not step.matched]


def load_trace(path: str | Path) -> list[dict[str, Any]]:
    """The records of a trace file, in order. A bad line stops the load."""
    records = []
    with Path(path).open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def replay_trace(path: str | Path, runner: StepRunner) -> ReplayReport:
    """Replay every recorded step through ``runner`` and compare the two."""
    records = load_trace(path)
    task = next((r for r in records if r.get("kind") == "task"), {})
    report = ReplayReport(trace_id=str(task.get("id") or Path(path).stem))
    for record in records:
        if record.get("kind") != "step":
            continue
        replayed = runner(record)
        replayed_action = replayed.get("action")
        replayed_sha = replayed.get("screen_sha256")
        recorded_action = record.get("action")
        recorded_sha = record.get("screen_sha256")
        mismatch = ""
        if replayed_action != recorded_action:
            mismatch = "action differs"
        elif replayed_sha != recorded_sha:
            mismatch = "screenshot differs"
        report.steps.append(
            StepReplay(
                step_no=int(record.get("step") or 0),
                recorded_action=recorded_action,
                replayed_action=replayed_action,
                recorded_sha256=recorded_sha,
                replayed_sha256=replayed_sha,
                matched=not mismatch,
                mismatch=mismatch,
            )
        )
    return report


def replay_single_step(path: str | Path, step_no: int, runner: StepRunner) -> StepReplay | None:
    """Re-run one recorded step. None when the trace has no such step."""
    for record in load_trace(path):
        if record.get("kind") == "step" and int(record.get("step") or 0) == step_no:
            replayed = runner(record)
            replayed_action = replayed.get("action")
            replayed_sha = replayed.get("screen_sha256")
            recorded_action = record.get("action")
            recorded_sha = record.get("screen_sha256")
            mismatch = ""
            if replayed_action != recorded_action:
                mismatch = "action differs"
            elif replayed_sha != recorded_sha:
                mismatch = "screenshot differs"
            return StepReplay(
                step_no=step_no,
                recorded_action=recorded_action,
                replayed_action=replayed_action,
                recorded_sha256=recorded_sha,
                replayed_sha256=replayed_sha,
                matched=not mismatch,
                mismatch=mismatch,
            )
    return None


__all__ = ["ReplayReport", "StepReplay", "load_trace", "replay_single_step", "replay_trace"]
