"""A sub-agent squad: one steward, three specialists, one approval gate.

The squad is an OPTIONAL mode.  By default nanoMuse runs a single
steward; a squad only exists when the caller builds one.  Inside a
squad, a coordinator fans work out to three specialists (research,
execute, verify) and the specialists talk back over frames shaped like
Hub frames, so the same wire shape carries both device to device and
agent to agent traffic.

The one rule that may never bend: approvals belong to the steward
alone.  A specialist cannot request an approval, cannot answer one,
and cannot run a sensitive action without an approval token the
steward issued.  Every refusal is written to the squad audit log, so a
bypass attempt leaves evidence even when it fails.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any


class SquadRefusal(RuntimeError):
    """A specialist tried to do something only the steward may do."""


@dataclass(frozen=True)
class SquadFrame:
    """One message on the squad bus, shaped like a Hub frame."""

    kind: str  # "call" | "event" | "result"
    sender: str
    recipient: str
    action: str
    body: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.kind,
            "from": self.sender,
            "to": self.recipient,
            "action": self.action,
            "body": self.body,
        }


class SquadBus:
    """In-memory frame bus.  Frames are delivered in order, to the
    named recipient only; a frame addressed to nobody in the squad is
    dropped and counted, never broadcast."""

    def __init__(self) -> None:
        self._inboxes: dict[str, list[SquadFrame]] = {}
        self.dropped: int = 0

    def register(self, member_id: str) -> None:
        self._inboxes.setdefault(member_id, [])

    def send(self, frame: SquadFrame) -> bool:
        inbox = self._inboxes.get(frame.recipient)
        if inbox is None:
            self.dropped += 1
            return False
        inbox.append(frame)
        return True

    def drain(self, member_id: str) -> list[SquadFrame]:
        inbox = self._inboxes.setdefault(member_id, [])
        frames = list(inbox)
        inbox.clear()
        return frames


@dataclass
class ApprovalToken:
    """Proof the steward approved one sensitive action, once."""

    token: str
    action: str
    target: str
    issued_to: str
    used: bool = False


class ApprovalGate:
    """The steward's approval desk.

    Only the steward's identity may request or answer approvals, and
    only a token this gate issued (unused, for this action and target,
    to this specialist) unlocks a sensitive action.  A forged token, a
    reused token, or a token presented by the wrong specialist is
    refused, and the refusal is audited.
    """

    STEWARD = "steward"

    def __init__(self) -> None:
        self._tokens: dict[str, ApprovalToken] = {}
        self.audit: list[dict[str, Any]] = []

    def _log(self, event: str, actor: str, detail: str) -> None:
        self.audit.append(
            {"seq": len(self.audit), "event": event, "actor": actor, "detail": detail}
        )

    def request_approval(self, actor: str, action: str, target: str) -> ApprovalToken:
        if actor != self.STEWARD:
            self._log("approval_request_refused", actor, f"{action}:{target}")
            raise SquadRefusal(f"only the steward may request an approval, not {actor!r}")
        token = ApprovalToken(
            token=secrets.token_hex(16),
            action=action,
            target=target,
            issued_to=self.STEWARD,
        )
        self._tokens[token.token] = token
        self._log("approval_issued", actor, f"{action}:{target}")
        return token

    def issue_to(self, actor: str, token: ApprovalToken, specialist: str) -> ApprovalToken:
        """Hand a steward-issued token to one specialist for one action."""
        if actor != self.STEWARD:
            self._log("approval_handoff_refused", actor, specialist)
            raise SquadRefusal("only the steward may hand an approval to a specialist")
        handed = ApprovalToken(
            token=token.token,
            action=token.action,
            target=token.target,
            issued_to=specialist,
        )
        self._tokens[token.token] = handed
        return handed

    def confirm(self, actor: str, token_value: str) -> bool:
        """The steward confirms an approval.  Anyone else is refused."""
        if actor != self.STEWARD:
            self._log("approval_confirm_refused", actor, token_value[:8])
            raise SquadRefusal(f"only the steward may confirm an approval, not {actor!r}")
        return token_value in self._tokens

    def run_sensitive(self, actor: str, token_value: str, action: str, target: str) -> bool:
        """Run a sensitive action only with a live token for this actor,
        action and target.  A valid token is spent on use."""
        token = self._tokens.get(token_value)
        if (
            token is None
            or token.used
            or token.issued_to != actor
            or token.action != action
            or token.target != target
        ):
            self._log("sensitive_action_refused", actor, f"{action}:{target}")
            return False
        token.used = True
        self._log("sensitive_action_run", actor, f"{action}:{target}")
        return True


@dataclass
class Specialist:
    """One squad member.  Specialists do work; they never approve."""

    member_id: str
    role: str  # "research" | "execute" | "verify"
    bus: SquadBus

    def report(self, recipient: str, action: str, body: dict[str, Any]) -> bool:
        return self.bus.send(
            SquadFrame(
                kind="event",
                sender=self.member_id,
                recipient=recipient,
                action=action,
                body=body,
            )
        )


class Squad:
    """Coordinator plus three specialists, defaulting to no squad at all.

    ``Squad.single()`` is the default mode: no bus, no specialists, the
    steward alone.  ``Squad.form()`` builds the optional squad mode.
    """

    ROLES = ("research", "execute", "verify")

    def __init__(self, *, formed: bool) -> None:
        self.formed = formed
        self.bus = SquadBus()
        self.gate = ApprovalGate()
        self.specialists: dict[str, Specialist] = {}
        self.created_at = time.time()
        if formed:
            self.bus.register(ApprovalGate.STEWARD)
            for role in self.ROLES:
                member_id = f"specialist:{role}"
                self.bus.register(member_id)
                self.specialists[role] = Specialist(member_id, role, self.bus)

    @classmethod
    def single(cls) -> Squad:
        return cls(formed=False)

    @classmethod
    def form(cls) -> Squad:
        return cls(formed=True)

    def dispatch(self, role: str, action: str, body: dict[str, Any]) -> bool:
        """The coordinator (steward side) sends work to one specialist."""
        if not self.formed:
            return False
        specialist = self.specialists.get(role)
        if specialist is None:
            return False
        return self.bus.send(
            SquadFrame(
                kind="call",
                sender=ApprovalGate.STEWARD,
                recipient=specialist.member_id,
                action=action,
                body=body,
            )
        )
