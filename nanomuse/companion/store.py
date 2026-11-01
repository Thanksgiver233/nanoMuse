"""Persistence for the companion runtime: tasks and working states on disk.

The Phase 4 runtime kept everything in memory, which is fine for a
simulation and useless for an always-on process: a restart would lose
every scheduled task and every handed-over task state.  This store
keeps both in one JSON file under the data directory:

- scheduled tasks, with their ran flags and results, so a task that
  ran before the restart never runs twice after it;
- the latest working state per task id, so a task handed over before
  the restart can still be resumed after it.

Writes are atomic (write to a sibling temp file, then rename), so a
crash mid-write leaves the previous good file, never a torn one.  A
file that does not parse loads as empty and is counted, the same
posture the cost ledger takes with a damaged line.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from nanomuse.companion.runtime import ScheduledTask, WorkingState


class CompanionStore:
    """One JSON file holding the companion's tasks and working states."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.corrupt = False
        self._tasks: dict[str, ScheduledTask] = {}
        self._working: dict[str, WorkingState] = {}
        self._load()

    # ------------------------------------------------------------------ io
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw: dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8"))
            for row in raw.get("tasks", []):
                task = ScheduledTask(**row)
                self._tasks[task.task_id] = task
            for task_id, frame in raw.get("working", {}).items():
                self._working[task_id] = WorkingState.from_frame(frame)
        except (ValueError, KeyError, TypeError):
            self.corrupt = True
            self._tasks = {}
            self._working = {}

    def _save(self) -> None:
        payload = {
            "tasks": [asdict(task) for task in self._tasks.values()],
            "working": {task_id: state.to_frame() for task_id, state in self._working.items()},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    # ------------------------------------------------------------------ tasks
    def put_task(self, task: ScheduledTask) -> None:
        self._tasks[task.task_id] = task
        self._save()

    def tasks(self) -> list[ScheduledTask]:
        return list(self._tasks.values())

    # ------------------------------------------------------------------ working states
    def put_working(self, state: WorkingState) -> None:
        self._working[state.task_id] = state
        self._save()

    def working(self, task_id: str) -> WorkingState | None:
        return self._working.get(task_id)

    def working_states(self) -> list[WorkingState]:
        return list(self._working.values())
