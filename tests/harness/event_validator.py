"""Event validation checks used by the SIM harness."""

from __future__ import annotations

import json
from typing import Any

from sim_server.schema_utils import validate_event_against_schema_like_rules


def parse_ndjson(content: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def validate_event_stream(events: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    last_seq = -1
    last_ts = ""
    last_turn = 0
    seen_end = False

    for idx, ev in enumerate(events):
        if seen_end:
            errors.append("event_after_episode_end")
        errs = validate_event_against_schema_like_rules(ev)
        errors.extend([f"event[{idx}]::{e}" for e in errs])

        seq = int(ev.get("event_seq", -1))
        if seq != last_seq + 1:
            errors.append(f"event_seq_gap_or_duplicate:{last_seq}->{seq}")
        last_seq = seq

        ts = str(ev.get("ts", ""))
        if last_ts and ts < last_ts:
            errors.append("timestamp_not_monotonic")
        last_ts = ts

        if ev.get("event_type") == "conversation":
            turn = int(ev.get("turn", 0))
            if turn != last_turn + 1:
                errors.append(f"conversation_turn_not_sequential:{last_turn}->{turn}")
            last_turn = turn

        if ev.get("event_type") == "meta" and seq != 0:
            errors.append("meta_not_first")

        if ev.get("event_type") == "episode_end":
            seen_end = True

    if events and events[-1].get("event_type") != "episode_end":
        errors.append("episode_end_not_last")

    return errors
