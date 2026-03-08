"""Phase 1 SIM + agents orchestrator with live/record/replay modes."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any

from agents import CallTakerAgent, CallerAgent, QAEvaluatorAgent
from sim_server import SimulationEngine
from sim_server.schema_utils import load_json
from tests.harness.event_validator import parse_ndjson, validate_event_stream


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize_events_for_replay(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cleaned = json.loads(json.dumps(events))
    for ev in cleaned:
        ev.pop("ts", None)
        ev.pop("tool_call_id", None)
        ev.pop("request_id", None)
        ev.pop("execution_id", None)
        ev.pop("incident_id", None)
        ev.pop("duration_ms", None)
        ev.pop("latency_ms", None)
        if isinstance(ev.get("detail"), dict):
            ev["detail"].pop("request_id", None)
            ev["detail"].pop("latency_ms", None)
        if isinstance(ev.get("response"), dict):
            ev["response"].pop("latency_ms", None)
    return cleaned


class SimEpisodeRunner:
    def __init__(self, root: Path, output_dir: Path, mode: str, replay_dir: Path | None = None) -> None:
        self.root = root
        self.output_dir = output_dir
        self.mode = mode
        self.replay_dir = replay_dir

    def run_episode(
        self,
        scenario_name: str,
        caller_file: str,
        incident_file: str,
        qa_file: str,
        max_turns: int = 20,
        calltaker_config: dict[str, Any] | None = None,
        qa_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        caller = load_json(self.root / caller_file)
        incident = load_json(self.root / incident_file)
        incident = json.loads(json.dumps(incident))
        incident["max_turns"] = max_turns
        qa_template = load_json(self.root / qa_file)

        engine = SimulationEngine(execution_id=f"sim-agents-{scenario_name}")
        load = engine.admin_load_scenario(
            scenario_id=scenario_name,
            caller_json=caller,
            incident_json=incident,
            qa_template_id=str(qa_template.get("version", "003")),
            qa_template_json=qa_template,
        )
        incident_id = load["incident_id"]
        engine.episode_start(incident_id)

        caller_agent = CallerAgent(caller_json=caller, incident_json=incident)
        calltaker_agent = CallTakerAgent(incident_json=incident, **(calltaker_config or {}))
        qa_agent = QAEvaluatorAgent(qa_template_json=qa_template, **(qa_config or {}))

        event_cursor = 0
        replay_steps = self._load_replay_steps(scenario_name)
        recorded_steps: list[dict[str, Any]] = []

        while True:
            snapshot = engine.plant_get_state_snapshot(incident_id)
            if snapshot["episode_phase"] == "sealed":
                break

            system_events, event_cursor = self._system_events_since(engine, incident_id, event_cursor)
            ct_last = self._latest_calltaker_text(engine, incident_id)

            if replay_steps is not None:
                step = replay_steps[len(recorded_steps)]
                caller_text = step["caller_text"]
                caller_meta = step["caller_metadata"]
                ct_text = step["calltaker_text"]
                cad_updates = step.get("cad_updates") or {}
                end_call = bool(step.get("end_call", False))
                end_reason = step.get("end_reason")
            else:
                caller_text, caller_meta = caller_agent.next_turn(call_taker_text=ct_last, system_events=system_events)
                decision = calltaker_agent.next_turn(
                    caller_text=caller_text,
                    cad_state=snapshot["cad_state"],
                    system_events=system_events,
                )
                ct_text = decision.text
                cad_updates = decision.cad_updates
                end_call = decision.end_call
                end_reason = decision.end_reason
                if decision.parse_error:
                    engine.plant_emit_event(
                        {
                            "event_type": "system",
                            "incident_id": incident_id,
                            "subtype": "parse_error",
                            "turn": int(snapshot.get("record_version", 0)),
                            "text": "calltaker_parse_fallback",
                        }
                    )

            engine.caller_post_turn(incident_id=incident_id, text=caller_text, metadata=caller_meta)
            engine.calltaker_post_turn(incident_id=incident_id, text=ct_text, cad_updates=cad_updates)

            recorded_steps.append(
                {
                    "caller_text": caller_text,
                    "caller_metadata": caller_meta,
                    "calltaker_text": ct_text,
                    "cad_updates": cad_updates,
                    "end_call": end_call,
                    "end_reason": end_reason,
                }
            )

            if end_call:
                engine.calltaker_end_call(incident_id=incident_id, reason=end_reason or "other")

            if len(recorded_steps) > max_turns + 5:
                engine.calltaker_end_call(incident_id=incident_id, reason="max_turns", reason_detail="runner_safety_stop")

        ev_blob = engine.artifact_get(incident_id, "_events.ndjson")["content"]
        events = parse_ndjson(ev_blob)
        schema_errors = validate_event_stream(events)

        qa_score = qa_agent.evaluate(events=events, incident_type=str(incident.get("type", "Unknown")))

        run_dir = self._run_dir()
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / f"{scenario_name}_events.ndjson").write_text(ev_blob, encoding="utf-8")
        (run_dir / f"{scenario_name}_qa_score.json").write_text(json.dumps(qa_score, indent=2), encoding="utf-8")
        (run_dir / f"{scenario_name}_meta.json").write_text(
            json.dumps(
                {
                    "scenario": scenario_name,
                    "mode": self.mode,
                    "steps": len(recorded_steps),
                    "events": len(events),
                    "schema_errors": schema_errors,
                    "prompt_hashes": {
                        "caller": _sha256_text("caller-deterministic-prompt-v1"),
                        "calltaker": _sha256_text("calltaker-deterministic-prompt-v1"),
                        "qa": _sha256_text("qa-deterministic-prompt-v1"),
                    },
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        if self.mode == "record":
            self._write_replay_steps(scenario_name, recorded_steps)

        return {
            "scenario": scenario_name,
            "incident_type": incident.get("type"),
            "mode": self.mode,
            "episode_sealed": True,
            "turn_count": len([e for e in events if e.get("event_type") == "conversation"]),
            "dispatch_turn": _dispatch_turn(events),
            "termination_reason": _episode_end_reason(events),
            "schema_valid": len(schema_errors) == 0,
            "schema_errors": schema_errors,
            "qa_score": qa_score,
            "normalized_events_hash": hashlib.sha256(
                json.dumps(_normalize_events_for_replay(events), sort_keys=True).encode("utf-8")
            ).hexdigest(),
            "events": events,
        }

    def _run_dir(self) -> Path:
        stamp = dt.datetime.now().strftime("sim_agents_%Y%m%d_%H%M%S")
        return self.output_dir / stamp

    def _replay_path(self, scenario_name: str) -> Path:
        base = self.replay_dir or (self.root / "fixtures" / "sim")
        return base / f"{scenario_name}_replay.json"

    def _load_replay_steps(self, scenario_name: str) -> list[dict[str, Any]] | None:
        if self.mode != "replay":
            return None
        path = self._replay_path(scenario_name)
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_replay_steps(self, scenario_name: str, steps: list[dict[str, Any]]) -> None:
        path = self._replay_path(scenario_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(steps, indent=2), encoding="utf-8")

    def _latest_calltaker_text(self, engine: SimulationEngine, incident_id: str) -> str:
        transcript = engine.plant_get_transcript_since(incident_id, 0)["turns"]
        if not transcript:
            return ""
        return str(transcript[-1].get("call_taker", ""))

    def _system_events_since(
        self,
        engine: SimulationEngine,
        incident_id: str,
        cursor: int,
    ) -> tuple[list[dict[str, Any]], int]:
        events = engine.episode_events(incident_id)
        new_events = [ev for ev in events if int(ev.get("event_seq", -1)) > cursor]
        system_events = [ev for ev in new_events if ev.get("event_type") == "system"]
        new_cursor = max([cursor] + [int(ev.get("event_seq", cursor)) for ev in new_events])
        return system_events, new_cursor


def _dispatch_turn(events: list[dict[str, Any]]) -> int | None:
    for ev in events:
        if ev.get("event_type") in {"tool_call", "cad_patch_applied"}:
            args = ev.get("args") or {}
            cad_updates = args.get("cad_updates") if isinstance(args, dict) else None
            if ev.get("dispatch_triggered") is True:
                return int(ev.get("turn", 0))
            if isinstance(cad_updates, dict) and cad_updates.get("dispatch_triggered") is True:
                return int(ev.get("turn", 0))
            if isinstance(ev.get("payload"), dict) and ev["payload"].get("dispatch_triggered") is True:
                return int(ev.get("turn", 0))
    return None


def _episode_end_reason(events: list[dict[str, Any]]) -> str:
    for ev in reversed(events):
        if ev.get("event_type") == "episode_end":
            return str(ev.get("reason", "unknown"))
    return "unknown"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Phase 1 SIM + agents episodes")
    parser.add_argument("--root", default=".")
    parser.add_argument("--output-dir", default="tests/reports")
    parser.add_argument("--mode", choices=["live", "record", "replay"], default="live")
    parser.add_argument("--replay-dir", default="")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    output_dir = (root / args.output_dir).resolve()
    replay_dir = Path(args.replay_dir).resolve() if args.replay_dir else None
    runner = SimEpisodeRunner(root=root, output_dir=output_dir, mode=args.mode, replay_dir=replay_dir)

    scenarios = [
        ("phase1_fire", "fixtures/caller_cooperative_calm.json", "fixtures/incident_fire_residential.json", "fixtures/qaTemplate_003.json"),
        ("phase1_police", "fixtures/caller_cooperative_calm.json", "fixtures/incident_police_domestic.json", "fixtures/qaTemplate_003.json"),
        ("phase1_ems", "fixtures/caller_cooperative_calm.json", "fixtures/incident_ems_cardiac.json", "fixtures/qaTemplate_003.json"),
    ]

    rows = [runner.run_episode(*cfg) for cfg in scenarios]
    summary = {
        "mode": args.mode,
        "total": len(rows),
        "all_schema_valid": all(r["schema_valid"] for r in rows),
        "mean_turn_count": sum(r["turn_count"] for r in rows) / len(rows),
        "dispatch_turns": {r["scenario"]: r["dispatch_turn"] for r in rows},
        "qa_scores": {r["scenario"]: r["qa_score"]["normalized_score"] for r in rows},
        "termination_reasons": {r["scenario"]: r["termination_reason"] for r in rows},
        "rows": rows,
    }

    out = output_dir / "sim_agents_summary.json"
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"SIM+agents summary written: {out}")


if __name__ == "__main__":
    main()
