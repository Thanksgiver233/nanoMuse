"""The degradation ladder: screen, then browser, then API (Phase 2).

A Hands task starts on the screen because that is the only rung that can
see every app. When a step fails, the run does not silently keep tapping:
it steps down one rung, says so in the trace, and keeps the step budget.
The ladder is a decision record, not an executor: the browser and API
rungs are chosen by the agent loop that owns those tools, and a run that
reaches the last rung and still fails stops and hands over to the person.
"""

from __future__ import annotations

from dataclasses import dataclass, field

RUNGS: tuple[str, ...] = ("screen", "browser", "api")


@dataclass
class DegradationDecision:
    step: int
    from_rung: str
    to_rung: str
    reason: str


@dataclass
class DegradationLadder:
    """Where a run stands on the ladder, and every step it took down."""

    current: str = "screen"
    decisions: list[DegradationDecision] = field(default_factory=list)

    @property
    def exhausted(self) -> bool:
        return self.current == RUNGS[-1]

    def advance(self, step: int, reason: str) -> str | None:
        """Step down one rung after a failure. None when already at the last."""
        try:
            index = RUNGS.index(self.current)
        except ValueError:
            return None
        if index + 1 >= len(RUNGS):
            return None
        nxt = RUNGS[index + 1]
        self.decisions.append(
            DegradationDecision(step=step, from_rung=self.current, to_rung=nxt, reason=reason[:200])
        )
        self.current = nxt
        return nxt


__all__ = ["RUNGS", "DegradationDecision", "DegradationLadder"]
