"""Startup and idle memory profile of the Python runtime (Phase 2, desktop slimming 1).

Measures only, and changes nothing: how long the runtime takes to import,
how much memory the import peaks at, and how much a just-imported process
holds while idle. No service is started, no key is used, no network is
touched. Run with the project virtualenv's Python.

Usage: python scripts/profile_startup.py --out phase2/logs/profile.json
"""

from __future__ import annotations

import argparse
import json
import resource
import subprocess
import sys
import time
from pathlib import Path


def _run(cmd: list[str], timeout: int = 120) -> dict[str, float]:
    started = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    wall = time.monotonic() - started
    peak_kb = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    return {
        "wall_s": round(wall, 3),
        "peak_rss_mb": round(peak_kb / 1024, 1),
        "exit_code": proc.returncode,
    }


def _idle_rss(samples: int = 2, sleep_s: float = 4.0) -> dict[str, float]:
    code = f"import nanomuse.app, time; time.sleep({sleep_s})"
    proc = subprocess.Popen(
        [sys.executable, "-c", code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    readings: list[float] = []
    try:
        for _ in range(samples):
            time.sleep(sleep_s / (samples + 1))
            try:
                status = Path(f"/proc/{proc.pid}/status").read_text(encoding="utf-8")
            except OSError:
                break
            for line in status.splitlines():
                if line.startswith("VmRSS:"):
                    readings.append(round(int(line.split()[1]) / 1024, 1))
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    return {"idle_rss_mb_samples": readings}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--trials", type=int, default=3)
    args = parser.parse_args()
    result: dict[str, object] = {
        "python": sys.version.split()[0],
        "import_app": [
            _run([sys.executable, "-c", "import nanomuse.app"]) for _ in range(args.trials)
        ],
        "cli_help": [_run([sys.executable, "-m", "nanomuse", "--help"]) for _ in range(2)],
        "idle": _idle_rss(),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
