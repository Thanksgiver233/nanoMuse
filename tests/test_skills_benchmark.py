"""The five benchmark skills: each loads, each declares format v1, each states
its read-only rule in its instructions, and each names the tests that cover it.

A skill is instructions, not code: what is tested here is the contract the
instructions carry — the manifest, the declared tools, and the rule that the
job never writes. Behaviour of the underlying tools is covered by their own
tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nanomuse.config import SkillsSettings
from nanomuse.skills.library import BUILTIN_DIR, SkillLibrary

BENCHMARKS = {
    "email-digest": {
        "tools": ["read_emails", "recall"],
        "allowed": ["read_emails", "recall"],
        "must_say": ["Read-only", "never send"],
    },
    "schedule-brief": {
        "tools": ["calendar", "reminders", "recall"],
        "allowed": ["calendar", "reminders", "recall"],
        "must_say": ["Read-only", "never create"],
    },
    "notes-capture": {
        "tools": ["files", "remember", "recall"],
        "allowed": ["files", "remember", "recall"],
        "must_say": ["never write outside", "notes/"],
    },
    "drive-readonly": {
        # the drive itself is reached over MCP, not a runtime tool: allowed-tools
        # names only what the skill may invoke directly
        "tools": ["mcp:dropbox", "recall"],
        "allowed": ["recall"],
        "must_say": ["read-only", "never upload"],
    },
    "repo-readonly": {
        "tools": ["mcp:github", "recall"],
        "allowed": ["recall"],
        "must_say": ["read-only", "never commit"],
    },
}


@pytest.fixture
def library(tmp_path: Path) -> SkillLibrary:
    return SkillLibrary(SkillsSettings(), tmp_path / "own", BUILTIN_DIR)


@pytest.mark.parametrize("name", sorted(BENCHMARKS))
def test_benchmark_skill_loads_as_format_v1(library: SkillLibrary, name: str) -> None:
    skill = library.get(name)
    assert skill is not None, f"{name} did not load: {library.errors}"
    assert skill.version == 1
    assert skill.signature_ok is None  # unsigned: shipped without a signing workflow
    assert skill.tests == ["tests/test_skills_benchmark.py"]
    assert skill.enabled


@pytest.mark.parametrize("name", sorted(BENCHMARKS))
def test_benchmark_skill_declares_its_tools(library: SkillLibrary, name: str) -> None:
    skill = library.get(name)
    assert skill is not None
    assert skill.allowed_tools == BENCHMARKS[name]["allowed"]
    assert skill.permissions.tools == BENCHMARKS[name]["tools"]


@pytest.mark.parametrize("name", sorted(BENCHMARKS))
def test_benchmark_skill_states_its_read_only_rule(library: SkillLibrary, name: str) -> None:
    skill = library.get(name)
    assert skill is not None
    for phrase in BENCHMARKS[name]["must_say"]:
        assert phrase.lower() in skill.body.lower(), f"{name} never says {phrase!r}"


def test_benchmark_skills_do_not_claim_network_or_files_they_do_not_need(
    library: SkillLibrary,
) -> None:
    for name in BENCHMARKS:
        skill = library.get(name)
        assert skill is not None
        assert skill.permissions.network == []
        if name != "notes-capture":
            assert skill.permissions.files == []
        else:
            assert skill.permissions.files == ["notes"]


def test_drive_and_repo_skills_keep_tokens_behind_placeholders(
    library: SkillLibrary,
) -> None:
    for name, provider in (("drive-readonly", "dropbox"), ("repo-readonly", "github")):
        skill = library.get(name)
        assert skill is not None
        assert "{{oauth:" + provider + "}}" in skill.body
