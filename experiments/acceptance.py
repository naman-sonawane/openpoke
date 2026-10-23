"""Run every acceptance layer and print one summary.

    python experiments/acceptance.py            # all layers (needs both servers running)
    python experiments/acceptance.py --offline  # unit tests only, no network or servers

Layers, cheapest first:
  1. unit        pytest: rules, guards, concurrency, every safety path with a fake model
  2. safety      triage_probe.py: awkward caller lines through the real safety screen
  3. calls       intake_scenarios.py: whole calls with LLM-played callers, 8 at a time
  4. ui          Playwright: the console in a real browser
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
NPX = "npx.cmd" if sys.platform == "win32" else "npx"

LAYERS = [
    ("unit", [sys.executable, "-m", "pytest", "-q"], REPO, r"(\d+) passed", r"(\d+) failed"),
    ("safety", [sys.executable, "experiments/triage_probe.py", "--runs", "2"], REPO,
     r"(\d+)/\d+ pass", r"(\d+) fail \("),
    ("calls", [sys.executable, "experiments/intake_scenarios.py", "--suite", "all", "--concurrency", "8"], REPO,
     r"(\d+)/\d+ scenarios passed", None),
    ("ui", [NPX, "playwright", "test"], REPO / "web", r"(\d+) passed", r"(\d+) failed"),
]


def main(offline: bool) -> int:
    results = []
    for name, command, cwd, passed_re, failed_re in LAYERS:
        if offline and name != "unit":
            continue
        print(f"\n### {name}: {' '.join(Path(c).name if i == 0 else c for i, c in enumerate(command))}", flush=True)
        started = time.perf_counter()
        proc = subprocess.run(command, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        output = proc.stdout + proc.stderr
        (REPO / "experiments" / f"acceptance_{name}.log").write_text(output, encoding="utf-8")
        passed = re.findall(passed_re, output)
        failed = re.findall(failed_re, output) if failed_re else []
        summary = f"{passed[-1] if passed else '?'} passed" + (f", {failed[-1]} failed" if failed else "")
        ok = proc.returncode == 0
        results.append((name, ok, summary, time.perf_counter() - started))
        print(f"{'PASS' if ok else 'FAIL'}  {summary}  ({results[-1][3]:.0f}s, log: experiments/acceptance_{name}.log)")

    print("\n" + "-" * 60)
    for name, ok, summary, seconds in results:
        print(f"{name:<8}{'PASS' if ok else 'FAIL':<6}{summary:<28}{seconds:>6.0f}s")
    return 0 if all(ok for _, ok, _, _ in results) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="unit tests only")
    sys.exit(main(parser.parse_args().offline))
