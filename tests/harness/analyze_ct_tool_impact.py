"""Analyze QA impact of call-taker tool-loop vs no-CAD profile from summary.csv."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from statistics import mean
from typing import Any


SYN_ID = "openai_synthetic_v1"
NOCAD_ID = "openai_gpt4o_mini_nocad"


def _to_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def analyze(summary_csv: Path) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    with summary_csv.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            rows.append(row)

    by_profile: dict[str, list[float]] = {SYN_ID: [], NOCAD_ID: []}
    by_cell: dict[tuple[str, str], dict[str, float | None]] = {}

    for r in rows:
        ct = str(r.get("calltaker_agent_id", ""))
        score = _to_float(r.get("qa_score"))
        caller_file = str(r.get("caller_file", ""))
        incident_file = str(r.get("incident_file", ""))
        key = (caller_file, incident_file)
        by_cell.setdefault(key, {SYN_ID: None, NOCAD_ID: None})
        if ct in by_profile and score is not None:
            by_profile[ct].append(score)
            by_cell[key][ct] = score

    paired_rows: list[dict[str, Any]] = []
    deltas: list[float] = []
    for key in sorted(by_cell.keys()):
        syn = by_cell[key][SYN_ID]
        nocad = by_cell[key][NOCAD_ID]
        delta = None
        if syn is not None and nocad is not None:
            delta = float(syn) - float(nocad)
            deltas.append(delta)
        paired_rows.append(
            {
                "caller_file": key[0],
                "incident_file": key[1],
                SYN_ID: syn,
                NOCAD_ID: nocad,
                "delta_syn_minus_nocad": delta,
            }
        )

    syn_scores = by_profile[SYN_ID]
    nocad_scores = by_profile[NOCAD_ID]
    syn_mean = mean(syn_scores) if syn_scores else None
    nocad_mean = mean(nocad_scores) if nocad_scores else None
    paired_mean_delta = mean(deltas) if deltas else None

    if paired_mean_delta is None:
        direction = "insufficient_data"
    elif paired_mean_delta > 0:
        direction = "synthetic_higher"
    elif paired_mean_delta < 0:
        direction = "nocad_higher"
    else:
        direction = "tie"

    return {
        "source_summary_csv": str(summary_csv),
        "total_runs": len(rows),
        "profile_counts": {
            SYN_ID: len(syn_scores),
            NOCAD_ID: len(nocad_scores),
        },
        "mean_qa_score_by_profile": {
            SYN_ID: syn_mean,
            NOCAD_ID: nocad_mean,
        },
        "paired_cells": paired_rows,
        "paired_mean_delta_syn_minus_nocad": paired_mean_delta,
        "direction": direction,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# Call-Taker Tool Impact Analysis")
    lines.append("")
    lines.append(f"- Source: `{report['source_summary_csv']}`")
    lines.append(f"- Total runs: {report['total_runs']}")
    lines.append(f"- Mean QA ({SYN_ID}): {report['mean_qa_score_by_profile'][SYN_ID]}")
    lines.append(f"- Mean QA ({NOCAD_ID}): {report['mean_qa_score_by_profile'][NOCAD_ID]}")
    lines.append(f"- Mean paired delta (synthetic - nocad): {report['paired_mean_delta_syn_minus_nocad']}")
    lines.append(f"- Direction: `{report['direction']}`")
    lines.append("")
    lines.append("## Per-Cell Pairing")
    lines.append("")
    lines.append("| caller_file | incident_file | synthetic | nocad | delta |")
    lines.append("|---|---|---:|---:|---:|")
    for row in report["paired_cells"]:
        lines.append(
            f"| `{row['caller_file']}` | `{row['incident_file']}` | {row[SYN_ID]} | {row[NOCAD_ID]} | {row['delta_syn_minus_nocad']} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze call-taker tool impact from experiment summary.csv")
    parser.add_argument("--summary-csv", required=True)
    parser.add_argument("--out-json", default="")
    parser.add_argument("--out-md", default="")
    args = parser.parse_args()

    summary_csv = Path(args.summary_csv).resolve()
    report = analyze(summary_csv)
    out_json = Path(args.out_json).resolve() if args.out_json else summary_csv.parent / "tool_impact_analysis.json"
    out_md = Path(args.out_md).resolve() if args.out_md else summary_csv.parent / "tool_impact_analysis.md"

    out_json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    out_md.write_text(render_markdown(report), encoding="utf-8")
    print(f"Analysis complete: {out_json} and {out_md}")


if __name__ == "__main__":
    main()
