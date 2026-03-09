"""Deterministic QA input bundling and report rendering helpers."""

from __future__ import annotations

import html
from typing import Any


def build_qa_input(*, events: list[dict[str, Any]], qa_template: dict[str, Any], incident_type: str) -> dict[str, Any]:
    meta = next((e for e in events if e.get("event_type") == "meta"), {})
    transcript = [
        {
            "turn": int(e.get("turn", 0) or 0),
            "call_taker": str(e.get("call_taker", "")),
            "caller": str(e.get("caller", "")),
            "ts": e.get("ts"),
        }
        for e in events
        if e.get("event_type") == "conversation"
    ]
    tool_calls = [_compact_tool_call(e) for e in events if e.get("event_type") == "tool_call"]
    system_events = [_compact_system_event(e) for e in events if e.get("event_type") == "system"]
    call_record = _last_value_call_record(tool_calls)
    normalize, normalize_to = _resolve_normalization(qa_template)
    template_out = dict(qa_template)
    template_out["normalize"] = normalize
    template_out["normalize_to"] = normalize_to

    return {
        "ruleset_id": str(qa_template.get("ruleset_id", "NENA-ANS1.107.1-2015-Add2.v1")),
        "discipline": str(incident_type).upper(),
        "template_version": str(qa_template.get("version", "000")),
        "template": template_out,
        "transcript": transcript,
        "call_record": call_record,
        "meta": {
            "scenario_id": meta.get("scenario_id"),
            "incident_id": meta.get("incident_id"),
            "incident_type": meta.get("incident_type"),
            "qa_template_id": meta.get("qa_template_id"),
        },
        "incident": {"type": incident_type},
        "tool_calls": tool_calls,
        "system_events": system_events,
        "metrics": {
            "dispatch": _dispatch_metrics(tool_calls),
        },
}


def build_qa_reports(
    *,
    qa_score: dict[str, Any],
    qa_template: dict[str, Any],
    scenario_id: str,
    incident_id: str,
) -> dict[str, str]:
    title = f"QA Score Sheet for {scenario_id} / {incident_id}"
    rows = _enriched_rows(qa_score, qa_template)
    grouped = _group_by_section(rows)
    section_order = _section_order(qa_template, qa_score)
    overall_awarded = float(qa_score.get("total_points_awarded", 0.0) or 0.0)
    overall_possible = float(qa_score.get("total_points_possible", 0.0) or 0.0)
    normalized_score = qa_score.get("normalized_score")
    norm_tag = ""
    if isinstance(normalized_score, (int, float)):
        norm_tag = f" (normalized={float(normalized_score):.2f})"

    md = [f"# {title}", "", f"**Overall:** {overall_awarded:.2f} / {overall_possible:.2f}{norm_tag}", ""]
    for sec in section_order:
        sec_rows = grouped.get(sec, [])
        if not sec_rows:
            continue
        sec_total = sum(float(r.get("awarded", 0.0) or 0.0) for r in sec_rows)
        md.append(f"## {sec} ({sec_total:.2f} pts)")
        md.append("")
        md.append("| ID | Question | Answer | Points | Max | Rationale |")
        md.append("|---|---|---|---:|---:|---|")
        for r in sec_rows:
            md.append(
                f"| {escape_md(r['id'])} | {escape_md(r['question'])} | {escape_md(r['answer'])} | "
                f"{float(r['awarded']):.2f} | {float(r['max_points']):.2f} | {escape_md(r['rationale'])} |"
            )
        md.append("")
    if qa_score.get("notes"):
        md.append("---")
        md.append("")
        md.append(f"**Notes:** {escape_md(str(qa_score.get('notes', '')))}")
    markdown = "\n".join(md).strip() + "\n"

    sec_blocks: list[str] = []
    for sec in section_order:
        sec_rows = grouped.get(sec, [])
        if not sec_rows:
            continue
        sec_total = sum(float(r.get("awarded", 0.0) or 0.0) for r in sec_rows)
        table_rows = "".join(
            (
                "<tr>"
                f"<td>{html.escape(str(r['id']))}</td>"
                f"<td>{html.escape(str(r['question']))}</td>"
                f"<td>{html.escape(str(r['answer']))}</td>"
                f"<td style='text-align:right'>{float(r['awarded']):.2f}</td>"
                f"<td style='text-align:right'>{float(r['max_points']):.2f}</td>"
                f"<td>{html.escape(str(r['rationale']))}</td>"
                "</tr>"
            )
            for r in sec_rows
        )
        sec_blocks.append(
            "<h2>"
            + html.escape(sec)
            + f" ({sec_total:.2f} pts)"
            + "</h2><table><thead><tr><th>ID</th><th>Question</th><th>Answer</th><th>Points</th><th>Max</th><th>Rationale</th></tr></thead><tbody>"
            + table_rows
            + "</tbody></table>"
        )
    notes_html = ""
    if qa_score.get("notes"):
        notes_html = f"<hr/><p><strong>Notes:</strong> {html.escape(str(qa_score.get('notes', '')))}</p>"
    report_html = (
        "<!doctype html><html><head><meta charset='utf-8'/>"
        f"<title>{html.escape(title)}</title>"
        "<style>"
        "body{font-family:Arial,sans-serif;max-width:980px;margin:28px auto;color:#223;} "
        "table{border-collapse:collapse;width:100%;margin-bottom:22px;} "
        "th,td{border:1px solid #d9dfeb;padding:8px;font-size:13px;vertical-align:top;} "
        "th{background:#f3f6fb;text-align:left;} h1,h2{margin-top:26px;}"
        "</style></head><body>"
        f"<h1>{html.escape(title)}</h1>"
        f"<p><strong>Overall:</strong> {overall_awarded:.2f} / {overall_possible:.2f}{html.escape(norm_tag)}</p>"
        + "".join(sec_blocks)
        + notes_html
        + "</body></html>"
    )

    return {"markdown": markdown, "html": report_html}


def _last_value_call_record(tool_calls: list[dict[str, Any]]) -> dict[str, Any]:
    record: dict[str, Any] = {}
    for tc in tool_calls:
        if tc.get("event_type") != "tool_call":
            continue
        args = tc.get("args")
        if not isinstance(args, dict):
            continue
        cad_updates = args.get("cad_updates")
        if isinstance(cad_updates, dict):
            record.update(cad_updates)
            continue
        updates = args.get("updates")
        if isinstance(updates, dict):
            record.update(updates)
    return record


def _compact_tool_call(ev: dict[str, Any]) -> dict[str, Any]:
    out = {
        "turn": int(ev.get("turn", 0) or 0),
        "tool_name": str(ev.get("tool_name", "")),
    }
    if isinstance(ev.get("args"), dict):
        out["args"] = ev.get("args")
    if isinstance(ev.get("fields_updated"), list):
        out["fields_updated"] = [str(x) for x in ev.get("fields_updated", [])]
    if "dispatch_triggered" in ev:
        out["dispatch_triggered"] = ev.get("dispatch_triggered")
    if "actor" in ev:
        out["actor"] = ev.get("actor")
    return out


def _compact_system_event(ev: dict[str, Any]) -> dict[str, Any]:
    out = {
        "turn": int(ev.get("turn", 0) or 0),
        "subtype": str(ev.get("subtype", "generic")),
        "text": str(ev.get("text", "")),
    }
    if ev.get("detail") is not None:
        out["detail"] = ev.get("detail")
    return out


def _dispatch_metrics(tool_calls: list[dict[str, Any]]) -> dict[str, Any]:
    for tc in tool_calls:
        args = tc.get("args", {})
        if isinstance(args, dict) and isinstance(args.get("cad_updates"), dict):
            if "dispatch_triggered" in args["cad_updates"]:
                return {
                    "triggered": bool(args["cad_updates"].get("dispatch_triggered")),
                    "turn": int(tc.get("turn", 0) or 0),
                }
    return {"triggered": None, "turn": None}


def _resolve_normalization(qa_template: dict[str, Any]) -> tuple[bool, float]:
    normalize_raw = qa_template.get("normalize")
    normalize_to_raw = qa_template.get("normalize_to")
    normalize_to: float | None
    if normalize_to_raw is None:
        normalize_to = None
    else:
        try:
            normalize_to = float(normalize_to_raw)
        except Exception:
            normalize_to = 0.0
    if normalize_raw is None:
        if normalize_to is None:
            return False, 0.0
        return (normalize_to > 0), max(0.0, normalize_to)
    normalize = bool(normalize_raw)
    if normalize_to is None:
        return normalize, 100.0
    return normalize, max(0.0, normalize_to)


def _section_order(qa_template: dict[str, Any], qa_score: dict[str, Any]) -> list[str]:
    incident_type = str(qa_score.get("incident_type", "")).upper()
    order: list[str] = []
    templates = qa_template.get("templates", {})
    if isinstance(templates, dict):
        common = templates.get("COMMON", {})
        if isinstance(common, dict):
            for sec in common.get("sections", []):
                if isinstance(sec, dict) and sec.get("name"):
                    order.append(str(sec["name"]))
        if incident_type and isinstance(templates.get(incident_type), dict):
            for sec in templates[incident_type].get("sections", []):
                if isinstance(sec, dict) and sec.get("name"):
                    order.append(str(sec["name"]))
    if not order:
        return sorted(set(str(r.get("section", "UNKNOWN")) for r in _enriched_rows(qa_score, qa_template)))
    return order


def _enriched_rows(qa_score: dict[str, Any], qa_template: dict[str, Any]) -> list[dict[str, Any]]:
    item_index = _template_item_index(qa_template, str(qa_score.get("incident_type", "")).upper())
    rows: list[dict[str, Any]] = []
    for row in qa_score.get("items", []) if isinstance(qa_score.get("items"), list) else []:
        if not isinstance(row, dict):
            continue
        item_id = str(row.get("id", "unknown"))
        tpl = item_index.get(item_id, {})
        rows.append(
            {
                "id": item_id,
                "question": str(tpl.get("question", row.get("question", ""))),
                "section": str(tpl.get("section", row.get("section", "UNKNOWN"))),
                "answer": str(row.get("answer", "")),
                "awarded": float(row.get("points_awarded", row.get("awarded", 0.0)) or 0.0),
                "max_points": float(row.get("points_possible", tpl.get("points", 0.0)) or 0.0),
                "rationale": str(row.get("rationale", "")),
            }
        )
    return rows


def _template_item_index(qa_template: dict[str, Any], incident_type: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    templates = qa_template.get("templates", {})
    if not isinstance(templates, dict):
        return out
    for key in ("COMMON", incident_type):
        section_group = templates.get(key)
        if not isinstance(section_group, dict):
            continue
        for sec in section_group.get("sections", []):
            if not isinstance(sec, dict):
                continue
            sec_name = str(sec.get("name", "UNKNOWN"))
            for item in sec.get("items", []):
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                out[str(item["id"])] = {
                    "question": str(item.get("question", "")),
                    "section": sec_name,
                    "points": float(item.get("points", 0.0) or 0.0),
                }
    return out


def _group_by_section(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        sec = str(row.get("section", "UNKNOWN"))
        out.setdefault(sec, []).append(row)
    return out


def escape_md(text: str) -> str:
    return str(text).replace("|", "\\|")
