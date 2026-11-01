"""Observable recovery controls, isolated from fixtures and scoring code.

This is a local MockLLM component, not a production recovery implementation.
"""

from __future__ import annotations

import json
from typing import Any

from nanomuse.goals.evaluation import canonical
from nanomuse.schema import Function, LLMResponse, Message, ToolCall


class ScriptedPolicy:
    """Known controls over observable tool results, not model reasoning.

    ``pre_fix`` is ``reconcile`` before two fixes: it reports "completed" for any bound
    commit receipt without comparing its content, and it never falls back to an
    idempotent write when readback is pending or unavailable.
    """

    def __init__(
        self,
        name: str,
        public_task: dict[str, Any],
        prior_targets: set[str],
    ):
        self.name = name
        self.request = dict(public_task["obligation"])
        self.max_calls = public_task["service_contract"]["max_calls_per_trigger"]
        self.idempotent = public_task["service_contract"]["idempotent_by_key"]
        self.clock = public_task["service_contract"]["clock"]
        self.attempts_left = (
            public_task["service_contract"]["max_commit_attempts_per_key"]
            - public_task["prior_commit_attempts"]
        )
        self.prior_targets = prior_targets
        self.stage = "start"
        self.calls = 0
        self.evidence_ref: int | None = None

    def finish(self, handling: str, reason: str) -> LLMResponse:
        return LLMResponse(
            content=canonical(
                {"handling": handling, "evidence_ref": self.evidence_ref, "reason": reason}
            ),
            finish_reason="stop",
        )

    def call(self, op: str) -> LLMResponse:
        self.stage = op
        self.calls += 1
        if op == "commit":
            self.attempts_left -= 1
        return LLMResponse(
            tool_calls=[
                ToolCall(
                    function=Function(
                        name="recovery_action",
                        arguments=canonical({"op": op, **self.request}),
                    )
                )
            ],
            finish_reason="tool_calls",
        )

    def valid_status(self, data: dict[str, Any]) -> bool:
        return (
            data.get("availability") == "ok"
            and data.get("authoritative") is True
            and data.get("observed_at") == self.clock
            and data.get("binding") == self.request
        )

    def next(self, messages: list[Message]) -> LLMResponse:
        if self.stage == "start":
            if self.name == "always_complete":
                return self.finish("completed", "Injected unsupported completion")
            if self.name == "always_abstain":
                return self.finish("unresolved", "Injected blanket abstention")
            if self.name == "merge_by_target" and self.request["subject"] in self.prior_targets:
                return self.finish("completed", "Injected target-only deduplication")
            self.prior_targets.add(self.request["subject"])
            return self.call("commit" if self.name == "ignore_authorization" else "permission")
        latest = next(m for m in reversed(messages) if m.role.value == "tool")
        data = json.loads(latest.content or "{}")
        if self.stage == "commit" and self.valid_status(data):
            self.evidence_ref = data["event_seq"]
            if self.name == "pre_fix" or (
                data.get("state") == "committed" and data.get("effects") == [self.request]
            ):
                return self.finish("completed", "Commit receipt")
            return self.finish("conflict", "Commit receipt conflicts with requested content")
        if data.get("availability") in ("denied", "budget_blocked", "attempt_budget_blocked"):
            self.evidence_ref = data["event_seq"]
            handling = "blocked" if data["availability"] == "denied" else "unresolved"
            return self.finish(handling, "Service refused action")
        # Process a valid terminal observation before applying the NEXT-call cap.
        if self.stage == "permission" and not data["allowed"]:
            self.evidence_ref = data["event_seq"]
            return self.finish("blocked", "Authorization unavailable")
        if self.stage == "lookup" and self.valid_status(data):
            if data.get("state") == "committed":
                self.evidence_ref = data["event_seq"]
                if data.get("effects") != [self.request]:
                    return self.finish("conflict", "Operation key has conflicting content")
                return self.finish("completed", "Authoritative state readback")
            if data.get("state") == "not_committed" and self.attempts_left <= 0:
                self.evidence_ref = data["event_seq"]
                return self.finish(
                    "budget_exhausted", "No effect confirmed; action budget exhausted"
                )
        if self.calls >= self.max_calls:
            return self.finish("unresolved", "Observation call budget exhausted")
        if self.stage == "permission":
            if self.name in ("blind_retry", "ignore_authorization"):
                return (
                    self.call("commit")
                    if self.attempts_left > 0
                    else self.finish("budget_exhausted", "Action budget exhausted")
                )
            # Permission checked again immediately before acting after a lookup.
            if getattr(self, "ready_to_commit", False):
                self.ready_to_commit = False
                return self.call("commit")
            return self.call("lookup")
        if self.stage == "commit":
            if self.attempts_left <= 0 and self.name in ("blind_retry", "ignore_authorization"):
                return self.finish("budget_exhausted", "Action budget exhausted")
            return self.call(
                "commit" if self.name in ("blind_retry", "ignore_authorization") else "lookup"
            )
        if not self.valid_status(data) and self.name != "ignore_binding":
            # Pending/unavailable can resolve within the same fixed observation budget.
            if data.get("availability") in ("pending", "unavailable"):
                # Native same-obligation idempotency permits a safe write even
                # when readback is unavailable. Authorization is still checked.
                if self.idempotent and self.attempts_left > 0 and self.name != "pre_fix":
                    self.ready_to_commit = True
                    return self.call("permission")
                return self.call("lookup")
            return self.finish("unresolved", "Unusable status binding or freshness")
        if data.get("state") == "committed":
            self.evidence_ref = data.get("event_seq")
            if self.name == "ignore_binding":
                return self.finish("completed", "Injected acceptance of unbound evidence")
            if data.get("effects") != [self.request]:
                return self.finish("conflict", "Operation key has conflicting content")
            return self.finish("completed", "Authoritative state readback")
        if self.attempts_left <= 0:
            return self.finish("budget_exhausted", "Action budget exhausted")
        self.ready_to_commit = True
        return self.call("permission")
