#!/usr/bin/env python3
"""The standard 8 hour companion soak: one server, one process, no restarts.

This driver is the Gate 6 long-run test for the always-on companion
(Phase 6). It runs on the target machine itself (a persistent cloud
computer or a VPS), next to the checkout it tests:

1. It writes an isolated HOME and a config that enables the companion
   (no Telegram token: the soak covers the runtime and the server, the
   channel smoke test is a separate Gate item).
2. It seeds N probe tasks evenly across the window, through the
   companion's own store, before the server starts.
3. It starts the real server (`python -m nanomuse serve`) as a
   subprocess and heartbeats every minute: process alive, /api/health
   answering, how many probe tasks have run, resident memory.
4. At the end of the window it verifies every probe task ran, and
   writes report.md next to heartbeat.jsonl and server.log.

Verdict PASS needs all of: the server process never exited during the
window, every probe task ran, at least 99% of health checks answered.
A crash is recorded, never hidden and never restarted away: this is
the standard edition of the soak, one continuous process. (A
restart-resilient variant exists as a different, separately reported
test; the two are not interchangeable.)

Usage:
    python3 soak_companion.py --repo ~/nanoMuse --work ~/soak-run --hours 8
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

SEED_CODE = """
import sys
from pathlib import Path
from nanomuse.companion.runtime import CompanionRuntime, ScheduledTask
from nanomuse.companion.store import CompanionStore

store_path, start, window, count = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), int(sys.argv[4])
runtime = CompanionRuntime(enabled=True, store=CompanionStore(Path(store_path)))
runtime.start()
for i in range(count):
    runtime.schedule(
        ScheduledTask(
            task_id=f"soak-{i:02d}",
            text=f"soak probe {i + 1} of {count}",
            run_at=start + window * (i + 1) / count,
        )
    )
print(f"seeded {count} tasks")
"""


def health_ok(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=5) as resp:
            return bool(json.loads(resp.read()).get("ok"))
    except Exception:
        return False


def rss_kb(pid: int) -> int:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except Exception:
        pass
    return 0


def tasks_ran(store_file: Path) -> int:
    try:
        data = json.loads(store_file.read_text())
        return sum(1 for task in data.get("tasks", []) if task.get("ran"))
    except Exception:
        return -1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="path to the nanoMuse checkout")
    parser.add_argument("--work", required=True, help="soak working directory (created)")
    parser.add_argument("--hours", type=float, default=8.0)
    parser.add_argument("--tasks", type=int, default=16)
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--heartbeat", type=int, default=60, help="seconds between heartbeats")
    args = parser.parse_args()

    repo = Path(args.repo).expanduser().resolve()
    work = Path(args.work).expanduser().resolve()
    home = work / "home"
    data_dir = home / ".nanomuse"
    data_dir.mkdir(parents=True, exist_ok=True)
    store_file = data_dir / "companion.json"
    heartbeat_file = work / "heartbeat.jsonl"
    server_log = (work / "server.log").open("ab")
    window = args.hours * 3600
    start = time.time()

    config = work / "config.toml"
    config.write_text(
        '[llm]\nprovider = "openai"\nmodel = "deepseek-flash"\n'
        'base_url = "https://api.deepseek.com"\napi_key = "soak-dummy"\n\n'
        "[companion]\nenabled = true\npoll_seconds = 10\n\n"
        f'[server]\nhost = "127.0.0.1"\nport = {args.port}\nupdate_check = false\n'
    )
    # No proxy variables for the soak processes: the server only talks to
    # itself here, and inherited proxy settings have killed a startup
    # before now (httpx InvalidURL from a no_proxy entry it cannot parse).
    env = {
        key: value
        for key, value in os.environ.items()
        if key.lower() not in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")
    }
    env.update({"HOME": str(home), "PYTHONPATH": str(repo), "PYTHONUNBUFFERED": "1"})

    seeded = subprocess.run(
        [sys.executable, "-c", SEED_CODE, str(store_file), str(start), str(window), str(args.tasks)],
        env=env,
        capture_output=True,
        text=True,
    )
    if seeded.returncode != 0:
        print(seeded.stderr, file=sys.stderr)
        return 2
    print(seeded.stdout.strip())

    proc = subprocess.Popen(
        [
            sys.executable, "-m", "nanomuse", "serve",
            "--config", str(config), "--no-auth", "--no-qr", "-p", str(args.port),
        ],
        cwd=repo,
        env=env,
        stdout=server_log,
        stderr=subprocess.STDOUT,
    )
    print(f"server pid {proc.pid}, window {args.hours}h, {args.tasks} probe tasks")

    deadline = start + window
    beats = ok_beats = 0
    up_beats = up_ok = 0  # beats after the server first answered: startup is not downtime
    seen_up = False
    crashed_at: float | None = None
    rss_first = rss_last = rss_max = 0
    try:
        while time.time() < deadline:
            alive = proc.poll() is None
            ok = alive and health_ok(args.port)
            beats += 1
            ok_beats += ok
            if ok:
                seen_up = True
            if seen_up:
                up_beats += 1
                up_ok += ok
            rss = rss_kb(proc.pid) if alive else 0
            if rss:
                rss_first = rss_first or rss
                rss_last = rss
                rss_max = max(rss_max, rss)
            with heartbeat_file.open("a") as fh:
                fh.write(json.dumps({
                    "t": round(time.time() - start, 1),
                    "alive": alive,
                    "health_ok": ok,
                    "tasks_ran": tasks_ran(store_file),
                    "rss_kb": rss,
                }) + "\n")
            if not alive:
                crashed_at = time.time() - start
                break
            time.sleep(args.heartbeat)
        if crashed_at is None:
            # The last probe task falls due right at the deadline; give the
            # companion a few ticks to run it before the final count.
            time.sleep(45)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()

    ran = tasks_ran(store_file)
    health_ratio = up_ok / up_beats if up_beats else 0.0
    passed = crashed_at is None and ran == args.tasks and health_ratio >= 0.99
    lines = [
        "# Companion soak report (standard edition)",
        "",
        f"- Window: {args.hours} hours, one continuous server process (pid {proc.pid})",
        f"- Probe tasks: {ran} of {args.tasks} ran",
        f"- Server crash: {'none' if crashed_at is None else f'at {crashed_at:.0f}s into the window'}",
        f"- Health checks: {up_ok} of {up_beats} answered after startup ({health_ratio:.1%})",
        f"- Resident memory: first {rss_first} kB, last {rss_last} kB, max {rss_max} kB",
        f"- Verdict: {'PASS' if passed else 'FAIL'}",
        "",
        "PASS needs: no crash in the window, every probe task ran, "
        "and at least 99% of health checks answered.",
    ]
    (work / "report.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
