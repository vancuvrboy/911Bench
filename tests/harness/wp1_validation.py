"""WP1 validation runner: integration tests + descriptor-driven smoke test."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


def run_command(cmd: list[str], cwd: Path) -> dict[str, Any]:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    return {
        "cmd": cmd,
        "exit_code": proc.returncode,
        "output": proc.stdout,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run WP1 MCP validation bundle.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--output-dir", default="tests/results")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    output_dir = (root / args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = output_dir / f"wp1_validation_report_{timestamp}.json"

    steps: list[dict[str, Any]] = []
    steps.append(run_command([sys.executable, "-m", "unittest", "tests.test_mcp_server"], cwd=root))
    steps.append(
        run_command(
            [
                sys.executable,
                "-m",
                "tests.harness.mcp_smoke_client",
                "--spawn-local-server",
                "--root",
                str(root),
            ],
            cwd=root,
        )
    )

    ok = all(step["exit_code"] == 0 for step in steps)
    report = {
        "ok": ok,
        "timestamp": timestamp,
        "steps": steps,
    }
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"wp1-validation report: {report_path}")
    print(json.dumps({"ok": ok, "steps": [{"cmd": s["cmd"], "exit_code": s["exit_code"]} for s in steps]}, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

