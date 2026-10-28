#!/usr/bin/env python3
"""Scan logs, traces and outbound payloads for leaked credentials (Gate 3 item 1).

A credential that reaches a log line, a saved trace or an outbound message is
a leak even when nothing reads it: logs get copied, traces get attached to
bug reports, chat messages get forwarded. This script checks that none of
those surfaces carries a secret value. It checks shapes, not a vault: it
cannot know a value it has never seen, so a clean run means no *recognisable*
credential shape was found, and the rules below say exactly which shapes.

Usage::

    python scripts/scan_secrets.py logs/ traces/ outbound.json
    python scripts/scan_secrets.py --json logs/ | jq .

Exit code 0 means no finding, 1 means at least one finding, 2 means the
invocation was wrong. Findings print the file, the line and the rule, never
the matched value: the scanner's own output must not become the leak it
looks for.

Rules:

* ``private-key``: a PEM private key block.
* ``github-token``: a GitHub personal access or OAuth token.
* ``openai-key``: an OpenAI-style ``sk-`` key, or an Anthropic one.
* ``aws-key``: an AWS access key id.
* ``secret-assignment``: a secret-named key assigned a quoted value that is
  not a ``{{vault:NAME}}`` or ``{{oauth:provider}}`` placeholder. Placeholders
  are the only form a secret may take in these surfaces, so they pass.
* ``bearer-header``: an ``Authorization: Bearer`` value that is not a
  placeholder.

The strong rules (everything but ``secret-assignment`` and
``bearer-header``) are safe to run over the whole repository: no fixture
should carry a real-looking token. The full set is for the surfaces listed
above, where a quoted assignment is a leak even when the token shape is
unknown.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

STRONG_RULES = ("private-key", "github-token", "openai-key", "aws-key")

_RULES: list[tuple[str, re.Pattern[str], str]] = [
    (
        "private-key",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
        "a PEM private key block",
    ),
    (
        "github-token",
        re.compile(
            r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b"
        ),
        "a GitHub token",
    ),
    (
        "openai-key",
        re.compile(r"\bsk-[A-Za-z0-9]{20,}\b|\bsk-ant-[A-Za-z0-9_-]{20,}\b"),
        "an OpenAI or Anthropic API key",
    ),
    (
        "aws-key",
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        "an AWS access key id",
    ),
    (
        "secret-assignment",
        re.compile(
            r"(?i)\b(?:passw(?:or)?d|passwd|secret|api[_-]?key|auth[_-]?token|access[_-]?token)"
            r"\s*[:=]\s*[\"']([^\"'\s]{8,})[\"']"
        ),
        "a secret-named key assigned a literal value",
    ),
    (
        "bearer-header",
        re.compile(r"(?i)authorization:\s*bearer\s+(\S{8,})"),
        "an Authorization Bearer value",
    ),
]

_PLACEHOLDER = re.compile(r"\{\{(?:vault|oauth):[^}]+\}\}")
_SKIP_DIRS = {".git", ".venv", "__pycache__", "node_modules", ".mypy_cache", ".ruff_cache"}
_MAX_FILE_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    excerpt: str


def _is_placeholder(value: str) -> bool:
    return bool(_PLACEHOLDER.fullmatch(value.strip()))


def scan_text(text: str, path: str, rules: tuple[str, ...] | None = None) -> list[Finding]:
    """Findings in ``text``, one per matching line and rule. Values are never
    kept: the excerpt carries the rule name where the value was."""
    wanted = set(rules) if rules is not None else {name for name, _, _ in _RULES}
    findings: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for name, pattern, _description in _RULES:
            if name not in wanted:
                continue
            for match in pattern.finditer(line):
                if name in ("secret-assignment", "bearer-header"):
                    if _is_placeholder(match.group(1)):
                        continue
                excerpt = line[:200].replace(match.group(0), f"[{name} redacted]")
                findings.append(Finding(path, number, name, excerpt))
                break
    return findings


def _iter_files(targets: list[Path]) -> list[Path]:
    files: list[Path] = []
    for target in targets:
        if target.is_file():
            files.append(target)
        elif target.is_dir():
            for path in sorted(target.rglob("*")):
                if path.is_file() and not any(part in _SKIP_DIRS for part in path.parts):
                    files.append(path)
    return files


def scan_paths(targets: list[Path], rules: tuple[str, ...] | None = None) -> list[Finding]:
    findings: list[Finding] = []
    for path in _iter_files(targets):
        try:
            if path.stat().st_size > _MAX_FILE_BYTES:
                continue
            text = path.read_text("utf-8", errors="strict")
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        findings.extend(scan_text(text, str(path), rules))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan files for leaked credentials.")
    parser.add_argument("paths", nargs="+", type=Path, help="files or directories to scan")
    parser.add_argument("--json", action="store_true", help="print findings as JSON")
    parser.add_argument(
        "--rules",
        default=None,
        help="comma-separated rule names to run (default: all); "
        "strong rules are: " + ", ".join(STRONG_RULES),
    )
    args = parser.parse_args(argv)
    rules: tuple[str, ...] | None = None
    if args.rules:
        rules = tuple(name.strip() for name in args.rules.split(",") if name.strip())
        unknown = set(rules) - {name for name, _, _ in _RULES}
        if unknown:
            parser.error(f"unknown rules: {', '.join(sorted(unknown))}")
    missing = [str(p) for p in args.paths if not p.exists()]
    if missing:
        parser.error(f"no such path: {', '.join(missing)}")
    findings = scan_paths(args.paths, rules)
    if args.json:
        print(json.dumps([finding.__dict__ for finding in findings], ensure_ascii=False, indent=1))
    else:
        for finding in findings:
            print(f"{finding.path}:{finding.line}: {finding.rule}: {finding.excerpt}")
        if findings:
            print(f"{len(findings)} finding(s)", file=sys.stderr)
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
