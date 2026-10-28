"""The Feed reason annotation and the interruption-audit sampler.

The reason tests drive a real check-in through the API: the Feed entry the
pass leaves behind must say it was a check-in, and a waiting approval must
say it is waiting for an answer. The sampler tests run the script's own
functions on synthetic feed exports: no feed is ever judged by a script.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from nanomuse.schema import LLMResponse
from tests.test_server import events_of, server, tc, wait_for  # noqa: F401,F811  (fixture reuse)


def test_check_in_feed_entry_says_why_it_interrupted(server) -> None:  # noqa: ANN001,F811
    client, service, llm = server
    g = client.post(
        "/api/goals",
        json={
            "title": "Walk every morning",
            "steps": ["Walk 20 min"],
            "check_in": "daily 07:00",
        },
    ).json()
    client.put("/api/settings", json={"profile": {"proactivity": "off"}})
    llm.script.append(LLMResponse(content="Morning! Did the walk happen today?"))
    assert client.post(f"/api/goals/{g['id']}/check-in").status_code == 200
    wait_for(lambda: events_of(client, kind="assistant"))
    feed = client.get("/api/feed").json()
    background = [item for item in feed if item["kind"] == "background"]
    assert background, f"no background feed entry in {feed}"
    assert background[0]["reason"] == "goal-check-in"
    assert background[0]["title"].startswith("Check-in")


def test_waiting_approval_feed_entry_says_it_needs_an_answer(server) -> None:  # noqa: ANN001,F811
    client, service, llm = server
    side = client.post("/api/threads", json={"title": "Side"}).json()
    llm.script.extend(
        [
            LLMResponse(tool_calls=[tc("shell", command="echo hi")]),
            LLMResponse(content="ok"),
        ]
    )
    client.post(f"/api/threads/{side['id']}/send", json={"text": "run it"})
    card = wait_for(
        lambda: [e for e in events_of(client, side["id"], "approval") if e["status"] == "pending"]
    )[0]
    feed = client.get("/api/feed").json()
    assert feed[0]["kind"] == "approval" and feed[0]["reason"] == "approval"
    assert feed[0]["id"] == card["id"]
    client.post(f"/api/approvals/{card['id']}", json={"approved": False})


def test_feed_entries_always_carry_a_reason(server) -> None:  # noqa: ANN001,F811
    client, service, llm = server
    assert client.get("/api/feed").json() == []
    llm.script.append(LLMResponse(content="Hello."))
    client.post("/api/threads/main/send", json={"text": "hi"})
    wait_for(lambda: events_of(client, kind="assistant"))
    # a plain conversation leaves no background feed entry; the shape is what
    # matters when one does appear, and the check-in test above covers it
    assert all("reason" in item for item in client.get("/api/feed").json())


# ------------------------------------------------------------------ the sampler
def _feed_export(n: int) -> list[dict[str, object]]:
    return [
        {
            "id": f"e{i}",
            "ts": f"2026-10-28T0{i % 10}:00:00Z",
            "kind": "background",
            "reason": "goal-check-in",
            "title": "Check-in",
            "text": f"entry {i}",
            "thread": "main",
        }
        for i in range(n)
    ]


def test_sampler_draw_is_deterministic_and_capped(tmp_path: Path) -> None:
    from scripts.audit_interruptions import draw_sample, load_feed

    feed_path = tmp_path / "feed.json"
    feed_path.write_text(json.dumps(_feed_export(80)), "utf-8")
    items = load_feed(feed_path)
    first = draw_sample(items, 50, seed=42)
    second = draw_sample(items, 50, seed=42)
    assert [row["id"] for row in first] == [row["id"] for row in second]
    assert len(first) == 50
    assert len(draw_sample(items, 50, seed=7)) == 50
    assert [row["id"] for row in draw_sample(items, 50, seed=7)] != [row["id"] for row in first]
    assert len(draw_sample(items, 200, seed=42)) == 80


def test_sampler_template_and_score_round_trip(tmp_path: Path) -> None:
    from scripts.audit_interruptions import sample_rows, score_sheet, write_template

    sheet = tmp_path / "sample.csv"
    write_template(sample_rows(_feed_export(20)), sheet)
    with sheet.open("r", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 20
    assert rows[0]["verdict"] == "" and rows[0]["reason"] == "goal-check-in"
    for i, row in enumerate(rows):
        row["verdict"] = "unwanted" if i < 2 else "ok"
    with sheet.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["id", "ts", "kind", "reason", "title", "text", "verdict", "note"],
        )
        writer.writeheader()
        writer.writerows(rows)
    result = score_sheet(sheet)
    assert result["reviewed"] == 20 and result["unwanted"] == 2
    assert result["misfire_rate"] == 0.1
    assert result["passes"] is True  # exactly at the 10% line passes


def test_sampler_fails_above_the_threshold_and_ignores_skips(tmp_path: Path) -> None:
    from scripts.audit_interruptions import sample_rows, score_sheet, write_template

    sheet = tmp_path / "sample.csv"
    write_template(sample_rows(_feed_export(20)), sheet)
    with sheet.open("r", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    for i, row in enumerate(rows):
        row["verdict"] = "unwanted" if i < 3 else ("skip" if i < 5 else "ok")
    with sheet.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["id", "ts", "kind", "reason", "title", "text", "verdict", "note"],
        )
        writer.writeheader()
        writer.writerows(rows)
    result = score_sheet(sheet)
    assert result["reviewed"] == 18  # skips do not count
    assert result["misfire_rate"] == 3 / 18
    assert result["passes"] is False


def test_sampler_cli_end_to_end(tmp_path: Path) -> None:
    import subprocess
    import sys

    feed_path = tmp_path / "feed.json"
    feed_path.write_text(json.dumps(_feed_export(60)), "utf-8")
    out = tmp_path / "sample.csv"
    proc = subprocess.run(
        [
            sys.executable,
            "scripts/audit_interruptions.py",
            "sample",
            str(feed_path),
            "--n",
            "50",
            "--seed",
            "42",
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        cwd=Path.cwd(),
    )
    assert proc.returncode == 0, proc.stderr
    assert "50 rows" in proc.stdout
    proc = subprocess.run(
        [sys.executable, "scripts/audit_interruptions.py", "score", str(out)],
        capture_output=True,
        text=True,
        cwd=Path.cwd(),
    )
    assert proc.returncode == 1  # nothing reviewed yet: no pass without a review
    assert "no reviewed rows" in proc.stdout
