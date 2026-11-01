"""The dry-run evaluation harness (Phase 2): golden tasks without a real device.

Every task runs the real :class:`~nanomuse.phone.operator.PhoneOperator`
against a scripted device (screens and answers from the spec) and a
:class:`~nanomuse.llm.MockLLM`. Nothing here touches a real phone, a real
account or a real model, so the numbers it produces are harness numbers:
they prove the specs run, the traces record, the replay checks and the
checkpoints verify. They are not Hands success rates; those need a real
vision model and a real device, which Phase 2 does not run.

Desktop specs run through the same mobile dialect in this version: the
spec records the intended platform, but a desktop-dialect dry run needs a
``ComputerLink`` fixture this harness does not have yet. That gap is
recorded in the Phase 2 report, not papered over.
"""

from __future__ import annotations

import asyncio
import base64
import json
import struct
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nanomuse.config import GUISettings, Settings
from nanomuse.llm import MockLLM
from nanomuse.phone import PhoneLink
from nanomuse.phone.operator import PhoneOperator
from nanomuse.schema import LLMResponse
from nanomuse.sentinel import AuditLog, Sentinel
from nanomuse.tools.phone import PhoneAct
from nanomuse.ui import ApprovalDecision, ApprovalRequest

MAX_STEPS_DEFAULT = 10


def tiny_png(width: int = 39, height: int = 84) -> str:
    """A real (tiny) PNG of the given size, base64: one grey row, repeated."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body))
            + kind
            + body
            + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + b"\x80" * width for _ in range(height))
    return base64.b64encode(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    ).decode()


class ScriptedDevice:
    """A device on the other end of the link: screens and answers from a script."""

    def __init__(self, link: PhoneLink, screens: list[dict[str, Any]]) -> None:
        self.link = link
        self.screens = list(screens)
        self.acts: list[dict[str, Any]] = []
        self.device = link.attach(
            "conn-1",
            {
                "name": "Dry-run device",
                "platform": "mobilegym",
                "gui": True,
                "apps": [{"id": "wechat", "name": "微信"}, {"id": "alipay", "name": "支付宝"}],
                "screen": {"width": 390, "height": 844},
            },
            self.send,
        )

    def _current(self) -> dict[str, Any]:
        return self.screens[0] if len(self.screens) == 1 else self.screens.pop(0)

    async def send(self, msg: dict[str, Any]) -> None:
        assert msg["kind"] == "device_request"
        if msg["op"] == "screen":
            result = self._current()
        else:
            self.acts.append(dict(msg["params"]))
            result = {"note": "ok", "screen": self._current()}
        asyncio.get_running_loop().call_soon(
            self.link.resolve,
            {"kind": "device_result", "id": msg["id"], "ok": True, "result": result},
        )


class DryRunUI:
    """Approvals auto-approved: dry runs only, never a real device."""

    def __init__(self) -> None:
        self.warnings: list[str] = []

    def on_text_delta(self, text: str) -> None: ...
    def on_assistant_message(self, content: object, reasoning: object) -> None: ...
    def on_reasoning_delta(self, text: str) -> None: ...
    def on_tool_call(self, call: object, summary: str) -> None: ...
    def on_tool_result(self, call: object, result: object) -> None: ...
    def on_sentinel(self, decision: str, summary: str, reasons: list[str]) -> None: ...
    def info(self, message: str) -> None: ...

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    async def ask_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(approved=True, reason="dry run")

    async def ask_user(self, question: str) -> str:
        return ""


@dataclass
class EvalTask:
    id: str
    chain: str
    language: str
    platform: str
    app: str
    goal: str
    context: str = ""
    screens: list[dict[str, Any]] = field(default_factory=list)
    script: list[dict[str, Any]] = field(default_factory=list)
    expected_status: str = "done"
    expected_text_contains: str = ""
    checkpoints: list[dict[str, Any]] = field(default_factory=list)
    max_steps: int = MAX_STEPS_DEFAULT


@dataclass
class EvalResult:
    task_id: str
    passed: bool
    status: str
    steps: int
    message: str
    checkpoints_passed: int
    checkpoints_total: int
    failures: list[str] = field(default_factory=list)
    trace_id: str = ""
    trace_path: str = ""
    duration_ms: int = 0
    cost_usd: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "passed": self.passed,
            "status": self.status,
            "steps": self.steps,
            "message": self.message,
            "checkpoints": f"{self.checkpoints_passed}/{self.checkpoints_total}",
            "failures": self.failures,
            "trace_id": self.trace_id,
            "trace_path": self.trace_path,
            "duration_ms": self.duration_ms,
            "cost_usd": self.cost_usd,
        }


@dataclass
class SuiteReport:
    results: list[EvalResult] = field(default_factory=list)
    dry_run: bool = True

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "total": self.total,
            "passed": self.passed,
            "results": [r.to_dict() for r in self.results],
        }


def load_tasks(path: str | Path) -> list[EvalTask]:
    """Task specs from a JSON file (``nanomuse/hands/eval_tasks.json``)."""
    with Path(path).open(encoding="utf-8") as fh:
        raw = json.load(fh)
    tasks = []
    for item in raw:
        screens = [dict(s) for s in item.get("screens", [])]
        for screen in screens:
            if screen.get("screenshot") in (None, "TINY"):
                screen["screenshot"] = tiny_png()
        tasks.append(EvalTask(**{**item, "screens": screens}))
    return tasks


def _reply(entry: dict[str, Any]) -> LLMResponse:
    action = entry["action"]
    arguments = dict(entry.get("arguments", {}))
    arguments["action"] = action
    label = str(entry.get("label") or action)
    call = {"name": "mobile_use", "arguments": arguments}
    return LLMResponse(
        content=(
            f"Thought: {entry.get('thought') or 'look'}\nAction: {label}\n"
            f"<tool_call>\n{json.dumps(call, ensure_ascii=False)}\n</tool_call>"
        )
    )


def _checkpoints_match(
    checkpoints: list[dict[str, Any]], acts: list[dict[str, Any]]
) -> tuple[int, list[str]]:
    """Checkpoints matched in order against the recorded device actions."""
    passed = 0
    failures: list[str] = []
    cursor = 0
    for checkpoint in checkpoints:
        found = None
        for index in range(cursor, len(acts)):
            act = acts[index]
            if act.get("action") != checkpoint.get("action"):
                continue
            label = str(act.get("label") or "")
            want_label = checkpoint.get("label_contains")
            if want_label and str(want_label).lower() not in label.lower():
                continue
            want_text = checkpoint.get("text")
            if want_text and str(want_text) not in str(act.get("text") or ""):
                continue
            found = index
            break
        if found is None:
            failures.append(f"checkpoint not reached: {checkpoint}")
        else:
            passed += 1
            cursor = found + 1
    return passed, failures


async def run_task(task: EvalTask, root: Path) -> EvalResult:
    """Run one spec dry and verify its checkpoints. No device, no account, no key."""
    started = time.monotonic()
    work = root / task.id
    settings = Settings()
    settings.data_dir = work / "data"
    settings.agent.workspace = work / "workspace"
    settings.llm.api_key = "test"
    settings.llm.stream = False
    settings.ensure_dirs()
    link = PhoneLink(shots_dir=settings.agent.workspace / "screenshots")
    device = ScriptedDevice(link, task.screens)
    ui = DryRunUI()
    audit = AuditLog(settings.data_dir / "audit.jsonl", session_id="eval")
    sentinel = Sentinel(settings.sentinel, audit=audit, ui=ui)
    gui = GUISettings(max_steps=task.max_steps)
    act = PhoneAct(link=link, gui=gui)
    llm = MockLLM([_reply(entry) for entry in task.script])
    operator = PhoneOperator(
        link,
        gui,
        sentinel,
        act,
        ui,
        make_llm=lambda: llm,
        traces_dir=settings.data_dir / "phone-traces",
    )
    outcome = await operator.run(task.goal, context=task.context, app=task.app)
    failures: list[str] = []
    if outcome.status != task.expected_status:
        failures.append(f"status {outcome.status!r}, expected {task.expected_status!r}")
    if task.expected_text_contains and task.expected_text_contains not in outcome.message:
        failures.append(f"message {outcome.message!r} lacks {task.expected_text_contains!r}")
    passed_checks, check_failures = _checkpoints_match(task.checkpoints, device.acts)
    failures.extend(check_failures)
    trace_path = settings.data_dir / "phone-traces" / f"{outcome.trace_id}.jsonl"
    return EvalResult(
        task_id=task.id,
        passed=not failures,
        status=outcome.status,
        steps=outcome.steps,
        message=outcome.message,
        checkpoints_passed=passed_checks,
        checkpoints_total=len(task.checkpoints),
        failures=failures,
        trace_id=outcome.trace_id,
        trace_path=str(trace_path) if trace_path.exists() else "",
        duration_ms=int((time.monotonic() - started) * 1000),
    )


async def run_suite(tasks: list[EvalTask], root: Path) -> SuiteReport:
    """Run every spec in order, each in its own directory."""
    report = SuiteReport()
    for task in tasks:
        report.results.append(await run_task(task, root))
    return report


def write_report(report: SuiteReport, path: str | Path) -> Path:
    """The suite report as JSON, for the Phase 2 delivery record."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return out


__all__ = [
    "DryRunUI",
    "EvalResult",
    "EvalTask",
    "ScriptedDevice",
    "SuiteReport",
    "load_tasks",
    "run_suite",
    "run_task",
    "tiny_png",
    "write_report",
]
