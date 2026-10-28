"""Squad tests, including the red-team cases: a specialist must never
be able to approve, confirm, forge, or reuse its way past the gate."""

from __future__ import annotations

import pytest

from nanomuse.team.squad import ApprovalGate, Squad, SquadFrame, SquadRefusal


def test_single_steward_is_the_default_no_squad_forms() -> None:
    squad = Squad.single()
    assert squad.formed is False
    assert squad.specialists == {}
    assert squad.dispatch("research", "search", {}) is False


def test_a_formed_squad_has_three_specialists_on_the_bus() -> None:
    squad = Squad.form()
    assert sorted(squad.specialists) == ["execute", "research", "verify"]
    assert squad.dispatch("research", "search", {"q": "x"}) is True
    frames = squad.bus.drain("specialist:research")
    assert len(frames) == 1
    assert frames[0].kind == "call"
    assert frames[0].sender == "steward"
    assert frames[0].to_dict()["action"] == "search"


def test_a_frame_to_nobody_is_dropped_not_broadcast() -> None:
    squad = Squad.form()
    sent = squad.bus.send(SquadFrame("event", "steward", "nobody", "ping", {}))
    assert sent is False
    assert squad.bus.dropped == 1
    assert squad.bus.drain("specialist:research") == []


def test_a_specialist_cannot_request_an_approval() -> None:
    gate = ApprovalGate()
    with pytest.raises(SquadRefusal):
        gate.request_approval("specialist:execute", "shell", "rm")
    assert gate.audit[-1]["event"] == "approval_request_refused"
    assert gate.audit[-1]["actor"] == "specialist:execute"


def test_a_specialist_cannot_confirm_an_approval() -> None:
    gate = ApprovalGate()
    token = gate.request_approval("steward", "shell", "rm")
    with pytest.raises(SquadRefusal):
        gate.confirm("specialist:verify", token.token)
    assert gate.audit[-1]["event"] == "approval_confirm_refused"


def test_a_specialist_cannot_hand_a_token_to_itself() -> None:
    gate = ApprovalGate()
    token = gate.request_approval("steward", "shell", "rm")
    with pytest.raises(SquadRefusal):
        gate.issue_to("specialist:execute", token, "specialist:execute")
    assert gate.audit[-1]["event"] == "approval_handoff_refused"


def test_a_forged_token_runs_nothing() -> None:
    gate = ApprovalGate()
    assert gate.run_sensitive("specialist:execute", "forged-token", "shell", "rm") is False
    assert gate.audit[-1]["event"] == "sensitive_action_refused"


def test_a_token_for_the_wrong_specialist_action_or_target_is_refused() -> None:
    gate = ApprovalGate()
    token = gate.request_approval("steward", "shell", "rm")
    handed = gate.issue_to("steward", token, "specialist:execute")
    assert gate.run_sensitive("specialist:research", handed.token, "shell", "rm") is False
    assert gate.run_sensitive("specialist:execute", handed.token, "shell", "ls") is False
    assert gate.run_sensitive("specialist:execute", handed.token, "shell", "other") is False
    assert gate.audit[-1]["event"] == "sensitive_action_refused"


def test_a_token_is_single_use() -> None:
    gate = ApprovalGate()
    token = gate.request_approval("steward", "shell", "rm")
    handed = gate.issue_to("steward", token, "specialist:execute")
    assert gate.run_sensitive("specialist:execute", handed.token, "shell", "rm") is True
    assert gate.run_sensitive("specialist:execute", handed.token, "shell", "rm") is False


def test_the_steward_path_runs_and_is_audited() -> None:
    gate = ApprovalGate()
    token = gate.request_approval("steward", "shell", "rm")
    assert gate.confirm("steward", token.token) is True
    handed = gate.issue_to("steward", token, "specialist:execute")
    assert gate.run_sensitive("specialist:execute", handed.token, "shell", "rm") is True
    events = [entry["event"] for entry in gate.audit]
    assert events == ["approval_issued", "sensitive_action_run"]


def test_a_specialist_report_reaches_the_steward() -> None:
    squad = Squad.form()
    specialist = squad.specialists["verify"]
    assert specialist.report("steward", "checked", {"ok": True}) is True
    frames = squad.bus.drain("steward")
    assert frames[0].action == "checked"
    assert frames[0].body == {"ok": True}
