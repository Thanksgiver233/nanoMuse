"""The dry-run harness runs specs end to end and verifies checkpoints."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from nanomuse.hands.harness import load_tasks, run_suite, run_task

TASKS = Path(__file__).parent.parent / "nanomuse" / "hands" / "eval_tasks.json"


def test_the_suite_is_thirty_specs_three_chains_two_languages() -> None:
    tasks = load_tasks(TASKS)
    assert len(tasks) == 30
    assert Counter(t.chain for t in tasks) == {
        "search_fill": 10,
        "receive_create": 10,
        "transfer_verify": 10,
    }
    assert Counter(t.language for t in tasks) == {"zh": 15, "en": 15}
    assert Counter(t.platform for t in tasks) == {"android": 15, "desktop": 15}
    assert all(t.script and t.checkpoints and t.expected_text_contains for t in tasks)


async def test_harness_runs_a_spec_and_writes_its_trace(tmp_path: Path) -> None:
    tasks = load_tasks(TASKS)
    first = next(t for t in tasks if t.language == "zh")
    result = await run_task(first, tmp_path)
    assert result.passed, result.failures
    assert result.status == "done"
    assert result.checkpoints_passed == result.checkpoints_total == 2
    assert result.cost_usd == 0.0
    assert result.trace_path and Path(result.trace_path).exists()


async def test_harness_reports_a_spec_whose_expectation_is_wrong(tmp_path: Path) -> None:
    tasks = load_tasks(TASKS)
    task = next(t for t in tasks if t.language == "en")
    task.expected_text_contains = "a phrase the answer never says"
    result = await run_task(task, tmp_path)
    assert not result.passed
    assert any("lacks" in f for f in result.failures)


async def test_suite_runs_in_order_and_counts(tmp_path: Path) -> None:
    tasks = load_tasks(TASKS)[:3]
    report = await run_suite(tasks, tmp_path)
    assert report.total == 3
    assert report.passed == 3
    assert report.dry_run is True
