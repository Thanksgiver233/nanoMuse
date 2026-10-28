"""The credential scanner: every rule fires on its shape, placeholders pass,
and the scanner's own output never carries the value it found."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.scan_secrets import STRONG_RULES, scan_paths, scan_text

REPO = Path(__file__).parent.parent
SCRIPT = REPO / "scripts" / "scan_secrets.py"

_DIRTY = {
    "private-key": "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n",
    "github-token": "token = ghp_abcdefghij1234567890ABCD\n",
    "openai-key": 'openai_key = "sk-abcdefghij1234567890abcdefghij"\n',
    "aws-key": "aws_access_key_id = AKIAIOSFODNN7EXAMPLE\n",
    "secret-assignment": 'password = "correct-horse-battery"\n',
    "bearer-header": "Authorization: Bearer abcdefghij1234567890\n",
}

_CLEAN_SURFACES = {
    "audit.jsonl": (
        '{"ts": "2026-10-28T00:00:00.000Z", "event": "tool_call", "tool": "send_email",'
        ' "args": {"to": "str[16]"}, "seq": 1, "prev": "000000",'
        ' "entry_hash": "0123456789abcdef0123456789abcdef"}\n'
    ),
    "feed.json": (
        '[{"kind": "notice", "reason": "reminder", "title": "Stand up",'
        ' "body": "Time for your 15:00 break."}]\n'
    ),
    "telegram-outbound.json": (
        '{"chat_id": 123, "text": "Your approval card: send mail to boss@example.com?",'
        ' "reply_markup": {"inline_keyboard": [[{"text": "Allow",'
        ' "callback_data": "approve:1"}]]}}\n'
    ),
    "trace.jsonl": (
        '{"step": 1, "screenshot_sha256": "abcdef0123456789", "action": "tap",'
        ' "target": "str[12]"}\n'
    ),
}


def test_every_rule_fires_on_its_shape():
    for rule, sample in _DIRTY.items():
        findings = scan_text(sample, "sample.txt", rules=(rule,))
        assert len(findings) == 1, (rule, findings)
        assert findings[0].rule == rule
        assert findings[0].line == 1


def test_placeholders_are_not_leaks():
    text = (
        'api_key = "{{vault:OPENAI_API_KEY}}"\n'
        "Authorization: Bearer {{oauth:github}}\n"
        'token = "{{vault:GITHUB_TOKEN}}"\n'
    )
    assert scan_text(text, "sample.txt") == []


def test_clean_surfaces_have_zero_hits(tmp_path: Path):
    for name, text in _CLEAN_SURFACES.items():
        (tmp_path / name).write_text(text, "utf-8")
    assert scan_paths([tmp_path]) == []


def test_no_strong_rule_fires_anywhere_in_the_runtime_tree():
    findings = scan_paths([REPO / "nanomuse", REPO / "tests", REPO / "scripts"], rules=STRONG_RULES)
    # This file itself carries the dirty samples on purpose; every other hit
    # would be a real-looking token in the tree.
    others = [f for f in findings if not f.path.endswith("test_scan_secrets.py")]
    assert others == [], others
    assert findings, "the samples in this file should fire the strong rules"


def test_the_cli_reports_findings_without_printing_the_value(tmp_path: Path):
    dirty = tmp_path / "leak.log"
    secret = "sk-abcdefghij1234567890abcdefghij"
    dirty.write_text(f'openai_key = "{secret}"\n', "utf-8")
    run = subprocess.run(
        [sys.executable, str(SCRIPT), str(dirty)],
        capture_output=True,
        text=True,
        cwd=REPO,
        check=False,
    )
    assert run.returncode == 1
    assert secret not in run.stdout
    assert secret not in run.stderr
    assert "openai-key" in run.stdout


def test_the_cli_exits_zero_on_clean_input_and_two_on_a_bad_path(tmp_path: Path):
    clean = tmp_path / "clean.log"
    clean.write_text("nothing here but a {{vault:KEY}} placeholder\n", "utf-8")
    assert (
        subprocess.run(
            [sys.executable, str(SCRIPT), str(clean)],
            capture_output=True,
            text=True,
            cwd=REPO,
            check=False,
        ).returncode
        == 0
    )
    assert (
        subprocess.run(
            [sys.executable, str(SCRIPT), str(tmp_path / "missing.log")],
            capture_output=True,
            text=True,
            cwd=REPO,
            check=False,
        ).returncode
        == 2
    )


def test_the_cli_json_output_is_parseable(tmp_path: Path):
    dirty = tmp_path / "leak.log"
    dirty.write_text("aws_access_key_id = AKIAIOSFODNN7EXAMPLE\n", "utf-8")
    run = subprocess.run(
        [sys.executable, str(SCRIPT), "--json", str(dirty)],
        capture_output=True,
        text=True,
        cwd=REPO,
        check=False,
    )
    assert run.returncode == 1
    findings = json.loads(run.stdout)
    assert findings[0]["rule"] == "aws-key"
    assert "AKIAIOSFODNN7EXAMPLE" not in run.stdout


def test_short_values_do_not_count_as_assignments():
    assert scan_text('token = "tok-1"\n', "sample.txt") == []
