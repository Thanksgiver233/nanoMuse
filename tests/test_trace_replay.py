"""Trace hashes and replay: a recorded run can be checked step by step."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from nanomuse.phone.replay import (
    ReplayReport,
    load_trace,
    replay_single_step,
    replay_trace,
)
from nanomuse.phone.trace import Trace


def _screen(path: str | None) -> Any:
    return SimpleNamespace(to_dict=lambda: {"image": path}, image_path=path)


def _write_trace(tmp_path: Path) -> Path:
    trace = Trace.start(tmp_path, goal="tap the button", app="test")
    assert trace.path is not None
    return trace.path


def test_step_records_the_screenshot_hash(tmp_path: Path) -> None:
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"fake screenshot bytes")
    path = _write_trace(tmp_path)
    trace = Trace(id="pt-x", path=path)
    trace.step(1, _screen(str(shot)), error=None)
    records = load_trace(path)
    step = next(r for r in records if r["kind"] == "step")
    assert step["screen_sha256"] == hashlib.sha256(b"fake screenshot bytes").hexdigest()


def test_step_without_a_screenshot_records_no_hash(tmp_path: Path) -> None:
    path = _write_trace(tmp_path)
    trace = Trace(id="pt-x", path=path)
    trace.step(1, _screen(None), error=None)
    records = load_trace(path)
    step = next(r for r in records if r["kind"] == "step")
    assert step["screen_sha256"] is None


def _synthetic_trace(tmp_path: Path) -> Path:
    path = tmp_path / "pt-test.jsonl"
    records = [
        {"kind": "task", "id": "pt-test", "goal": "tap twice"},
        {
            "kind": "step",
            "step": 1,
            "action": "tap",
            "screen_sha256": "aaa",
            "params": {"action": "tap"},
        },
        {
            "kind": "step",
            "step": 2,
            "action": "answer",
            "screen_sha256": "bbb",
            "params": {"action": "answer"},
        },
        {"kind": "end", "status": "done"},
    ]
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def test_replay_matches_an_identical_rerun(tmp_path: Path) -> None:
    path = _synthetic_trace(tmp_path)

    def echo(record: dict[str, Any]) -> dict[str, Any]:
        return {"action": record["action"], "screen_sha256": record["screen_sha256"]}

    report: ReplayReport = replay_trace(path, echo)
    assert report.trace_id == "pt-test"
    assert (report.total, report.matched) == (2, 2)
    assert report.consistency == 1.0
    assert report.mismatches == []


def test_replay_lists_a_step_whose_action_or_screenshot_differs(tmp_path: Path) -> None:
    path = _synthetic_trace(tmp_path)

    def drift(record: dict[str, Any]) -> dict[str, Any]:
        if record["step"] == 1:
            return {"action": "swipe", "screen_sha256": "aaa"}
        return {"action": "answer", "screen_sha256": "zzz"}

    report = replay_trace(path, drift)
    assert report.consistency == 0.0
    assert [s.step_no for s in report.mismatches] == [1, 2]
    assert report.mismatches[0].mismatch == "action differs"
    assert report.mismatches[1].mismatch == "screenshot differs"


def test_replay_single_step_reruns_only_that_step(tmp_path: Path) -> None:
    path = _synthetic_trace(tmp_path)
    seen: list[int] = []

    def echo(record: dict[str, Any]) -> dict[str, Any]:
        seen.append(int(record["step"]))
        return {"action": record["action"], "screen_sha256": record["screen_sha256"]}

    step = replay_single_step(path, 2, echo)
    assert step is not None and step.matched and step.step_no == 2
    assert seen == [2]
    assert replay_single_step(path, 9, echo) is None
