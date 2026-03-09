"""Role-based agent provider registry for deterministic/replay/OpenAI agents.

Developer extension guide (plug-and-play profiles):
1) Add or update a profile YAML in `agents/config/` using the naming pattern:
   - `<role>.<profile_id>.yaml` where role is one of: `caller`, `calltaker`, `qa`.
2) Put profile metadata in YAML (single source of truth for non-builtin profiles):
   - `id`, `role`, `provider`, `mode`, `adapter`, `description`
   - plus runtime fields such as `model`, `temperature`, prompt text/files, limits, strategy flags.
3) Provider discovery:
   - `list_profiles()` and `get_profile()` load builtins + YAML profiles.
   - Builtins (`manual`, `deterministic_v1`, `replay`) remain code defaults for safety.
4) Adapter dispatch:
   - `create_caller_agent`, `create_calltaker_agent`, `create_qa_agent`
     select implementation by YAML `adapter` value.
   - If you can reuse an existing adapter, no Python changes are needed.
5) Adding a new adapter type (Python change required once):
   - Implement a class and constructor helper.
   - Add one adapter mapping branch in the relevant `create_*_agent` function.
   - After that, new profiles can use that adapter via YAML only.
6) Tool-capable agents:
   - Define tool schemas in class method `_tool_specs()`.
   - Implement behavior in `_exec_tool()`.
   - Execute tool loop inside `next_turn()`.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from agents.caller_agent import CallerAgent
from agents.calltaker_agent import CTDecision, CallTakerAgent
from agents.qa_agent import QAEvaluatorAgent


@dataclass(frozen=True)
class AgentProfile:
    id: str
    role: str
    provider: str
    model: str
    temperature: float
    description: str
    mode: str


_BUILTIN_CATALOG: list[AgentProfile] = [
    AgentProfile(
        id="manual",
        role="caller",
        provider="builtin",
        model="-",
        temperature=0.0,
        description="UI/manual caller turns only",
        mode="manual",
    ),
    AgentProfile(
        id="deterministic_v1",
        role="caller",
        provider="builtin",
        model="rule-based",
        temperature=0.3,
        description="Deterministic scripted caller",
        mode="callable",
    ),
    AgentProfile(
        id="replay",
        role="caller",
        provider="builtin",
        model="fixture-replay",
        temperature=0.0,
        description="Replay pre-recorded caller outputs",
        mode="replay",
    ),
    AgentProfile(
        id="manual",
        role="calltaker",
        provider="builtin",
        model="-",
        temperature=0.0,
        description="UI/manual call-taker turns only",
        mode="manual",
    ),
    AgentProfile(
        id="deterministic_v1",
        role="calltaker",
        provider="builtin",
        model="rule-based",
        temperature=0.0,
        description="Deterministic scripted call-taker",
        mode="callable",
    ),
    AgentProfile(
        id="replay",
        role="calltaker",
        provider="builtin",
        model="fixture-replay",
        temperature=0.0,
        description="Replay pre-recorded call-taker outputs",
        mode="replay",
    ),
    AgentProfile(
        id="manual",
        role="qa",
        provider="builtin",
        model="-",
        temperature=0.0,
        description="No QA evaluator",
        mode="manual",
    ),
    AgentProfile(
        id="deterministic_v1",
        role="qa",
        provider="builtin",
        model="rule-based",
        temperature=0.0,
        description="Deterministic QA evaluator",
        mode="callable",
    ),
    AgentProfile(
        id="replay",
        role="qa",
        provider="builtin",
        model="fixture-replay",
        temperature=0.0,
        description="Replay QA score fixture if available",
        mode="replay",
    ),
]


def _default_config_root() -> Path:
    return (Path(__file__).resolve().parent / "config").resolve()


def _yaml_load(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except Exception:
        return _minimal_yaml_load(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return _minimal_yaml_load(path)
    return raw if isinstance(raw, dict) else {}


def _parse_scalar(value: str) -> Any:
    v = value.strip()
    if not v:
        return ""
    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
        return v[1:-1]
    low = v.lower()
    if low in {"true", "false"}:
        return low == "true"
    if low in {"null", "none"}:
        return None
    try:
        if "." in v:
            return float(v)
        return int(v)
    except Exception:
        return v


def _minimal_yaml_load(path: Path) -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return out
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        if line.startswith(" ") or ":" not in line:
            i += 1
            continue
        key, rest = line.split(":", 1)
        key = key.strip()
        rest = rest.strip()
        if rest == "|":
            i += 1
            block: list[str] = []
            while i < n:
                nxt = lines[i]
                if not nxt:
                    block.append("")
                    i += 1
                    continue
                if not nxt.startswith(" "):
                    break
                block.append(nxt[2:] if nxt.startswith("  ") else nxt.lstrip())
                i += 1
            out[key] = "\n".join(block).rstrip()
            continue
        out[key] = _parse_scalar(rest)
        i += 1
    return out


def _load_external_profiles(config_root: str | Path | None) -> list[AgentProfile]:
    base = Path(config_root).resolve() if config_root is not None else _default_config_root()
    if not base.exists() or not base.is_dir():
        return []
    out: list[AgentProfile] = []
    for path in sorted(base.glob("*.yaml")):
        stem = path.stem
        if "." not in stem:
            continue
        role_from_name, id_from_name = stem.split(".", 1)
        if role_from_name not in {"caller", "calltaker", "qa"}:
            continue
        cfg = _yaml_load(path)
        role = str(cfg.get("role", role_from_name))
        if role not in {"caller", "calltaker", "qa"}:
            continue
        profile_id = str(cfg.get("id", id_from_name))
        provider = str(cfg.get("provider", "openai"))
        mode = str(cfg.get("mode", "callable"))
        model = str(cfg.get("model", "-"))
        try:
            temperature = float(cfg.get("temperature", 0.0))
        except Exception:
            temperature = 0.0
        description = str(cfg.get("description", f"Configured {role} profile"))
        out.append(
            AgentProfile(
                id=profile_id,
                role=role,
                provider=provider,
                model=model,
                temperature=temperature,
                description=description,
                mode=mode,
            )
        )
    return out


def _build_catalog(config_root: str | Path | None = None) -> list[AgentProfile]:
    by_key: dict[tuple[str, str], AgentProfile] = {(p.role, p.id): p for p in _BUILTIN_CATALOG}
    for p in _load_external_profiles(config_root):
        by_key[(p.role, p.id)] = p
    # Keep stable ordering by role then id for UI.
    return sorted(by_key.values(), key=lambda p: (p.role, p.id))


def list_profiles(*, config_root: str | Path | None = None) -> list[dict[str, Any]]:
    return [asdict(p) for p in _build_catalog(config_root=config_root)]


def get_profile(role: str, agent_id: str, *, config_root: str | Path | None = None) -> AgentProfile:
    for p in _build_catalog(config_root=config_root):
        if p.role == role and p.id == agent_id:
            return p
    raise ValueError(f"unknown agent profile: role={role}, id={agent_id}")


def is_replay(role: str, agent_id: str, *, config_root: str | Path | None = None) -> bool:
    return get_profile(role, agent_id, config_root=config_root).mode == "replay"


def is_manual(role: str, agent_id: str, *, config_root: str | Path | None = None) -> bool:
    return get_profile(role, agent_id, config_root=config_root).mode == "manual"


def create_caller_agent(
    agent_id: str,
    caller_json: dict[str, Any],
    incident_json: dict[str, Any],
    *,
    config_root: str | Path | None = None,
) -> CallerAgent | Any | None:
    profile = get_profile("caller", agent_id, config_root=config_root)
    if profile.mode in {"manual", "replay"}:
        return None
    if profile.provider == "builtin":
        return CallerAgent(caller_json=caller_json, incident_json=incident_json, temperature=profile.temperature)
    cfg = _load_agent_config(config_root=config_root, role="caller", agent_id=agent_id)
    adapter = str(cfg.get("adapter", "openai_caller_responses")).strip().lower()
    if adapter in {"openai_caller_responses", "openai_responses"}:
        return _create_openai_caller(profile, caller_json, incident_json, agent_config=cfg)
    raise ValueError(f"unsupported caller adapter: {adapter}")


def create_calltaker_agent(
    agent_id: str,
    incident_json: dict[str, Any],
    qa_template_json: dict[str, Any] | None = None,
    *,
    config_root: str | Path | None = None,
    **kwargs: Any,
) -> CallTakerAgent | Any | None:
    profile = get_profile("calltaker", agent_id, config_root=config_root)
    if profile.mode in {"manual", "replay"}:
        return None
    if profile.provider == "builtin":
        return CallTakerAgent(incident_json=incident_json, temperature=profile.temperature, **kwargs)
    # OpenAI call-taker profiles run incident-blind by default. They infer details
    # through caller dialogue and tool-returned runtime state, not seed injection.
    runtime_incident: dict[str, Any] = {}
    cfg = _load_agent_config(config_root=config_root, role="calltaker", agent_id=agent_id)
    adapter = str(cfg.get("adapter", "")).strip().lower()
    if not adapter:
        adapter = "openai_calltaker_synthetic" if "synthetic" in profile.id else "openai_calltaker_json"
    if adapter in {"openai_calltaker_synthetic", "openai_synthetic"}:
        return _create_openai_synthetic_calltaker(
            profile,
            runtime_incident,
            qa_template_json=qa_template_json or {},
            agent_config=cfg,
        )
    if adapter in {"openai_calltaker_json", "openai_chat_json"}:
        return _create_openai_calltaker(profile, runtime_incident, agent_config=cfg)
    raise ValueError(f"unsupported calltaker adapter: {adapter}")


def create_qa_agent(
    agent_id: str,
    qa_template_json: dict[str, Any],
    *,
    config_root: str | Path | None = None,
    **kwargs: Any,
) -> QAEvaluatorAgent | Any | None:
    profile = get_profile("qa", agent_id, config_root=config_root)
    if profile.mode in {"manual", "replay"}:
        return None
    if profile.provider == "builtin":
        return QAEvaluatorAgent(qa_template_json=qa_template_json, temperature=profile.temperature, **kwargs)
    cfg = _load_agent_config(config_root=config_root, role="qa", agent_id=agent_id)
    adapter = str(cfg.get("adapter", "openai_qa_json")).strip().lower()
    if adapter in {"openai_qa_json", "openai_chat_json"}:
        return _create_openai_qa(profile, qa_template_json, agent_config=cfg)
    raise ValueError(f"unsupported qa adapter: {adapter}")


def _load_agent_config(config_root: str | Path | None, role: str, agent_id: str) -> dict[str, Any]:
    base = Path(config_root).resolve() if config_root is not None else _default_config_root()
    path = base / f"{role}.{agent_id}.yaml"
    if not path.exists():
        return {}
    return _yaml_load(path)


def _create_openai_client() -> Any:
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not set")
    try:
        from openai import OpenAI  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("openai package not installed; install it to use OpenAI agent profiles") from exc
    return OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))


class OpenAICallerAgent:
    def __init__(
        self,
        model: str,
        temperature: float,
        caller_json: dict[str, Any],
        incident_json: dict[str, Any],
        agent_config: dict[str, Any] | None = None,
        agent_profile_id: str = "openai",
    ) -> None:
        self.client = _create_openai_client()
        cfg = agent_config or {}
        self.model = str(cfg.get("model", model))
        self.temperature = float(cfg.get("temperature", temperature))
        self.max_output_tokens = int(cfg.get("max_output_tokens", 120))
        self.use_previous_response_id = bool(cfg.get("use_previous_response_id", True))
        self.system_prompt = str(
            cfg.get(
                "system_prompt",
                "You are simulating a 911 caller in a real-time emergency scenario. "
                "Stay fully in character and respond only as the caller would speak.",
            )
        )
        self.caller_json = caller_json
        self.incident_json = incident_json
        self.agent_profile_id = agent_profile_id
        self.turn_index = 0
        self.previous_response_id: str | None = None
        self._seeded = False
        self._history: list[dict[str, str]] = []
        self._max_history_turns = int(cfg.get("max_history_turns", 10))
        self._fallback = CallerAgent(caller_json=caller_json, incident_json=incident_json, temperature=temperature)

    def _seed_packet(self) -> str:
        payload = {
            "caller_profile": self.caller_json,
            "incident_details": self.incident_json,
        }
        return (
            "Use this structured seed data for internal behavior only. "
            "Do not quote it or mention it directly.\n"
            f"seed_json={json.dumps(payload)}"
        )

    def _turn_update_packet(self, call_taker_text: str, system_events: list[dict[str, Any]]) -> str:
        self.turn_index += 1
        return (
            f"turn_index={self.turn_index}\n"
            "[SYSTEM]\n"
            f"recent_system_events={json.dumps(system_events[-5:])}\n"
            "[/SYSTEM]\n"
            f"call_taker_utterance={call_taker_text}\n"
            "Respond with caller speech only."
        )

    def _extract_text(self, resp: Any) -> str:
        output_text = str(getattr(resp, "output_text", "") or "").strip()
        if output_text:
            return output_text
        output = getattr(resp, "output", None)
        if isinstance(output, list):
            chunks: list[str] = []
            for item in output:
                content = getattr(item, "content", None)
                if isinstance(content, list):
                    for part in content:
                        txt = str(getattr(part, "text", "") or "").strip()
                        if txt:
                            chunks.append(txt)
            joined = "\n".join(chunks).strip()
            if joined:
                return joined
        return ""

    def _clean_speech(self, text: str) -> str:
        cleaned = re.sub(r"\*(.*?)\*", "", text)
        cleaned = re.sub(r"\[(.*?)\]", "", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned

    def _fallback_turn(self, call_taker_text: str, system_events: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
        return self._fallback.next_turn(call_taker_text=call_taker_text, system_events=system_events)

    def next_turn(self, call_taker_text: str, system_events: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
        turn_packet = self._turn_update_packet(call_taker_text, system_events)
        try:
            kwargs: dict[str, Any] = {
                "model": self.model,
                "temperature": self.temperature,
                "max_output_tokens": self.max_output_tokens,
            }

            if self.use_previous_response_id and self._seeded and self.previous_response_id:
                kwargs["previous_response_id"] = self.previous_response_id
                kwargs["input"] = [{"role": "user", "content": turn_packet}]
            else:
                input_msgs: list[dict[str, Any]] = [
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": self._seed_packet()},
                ]
                if self._history:
                    for item in self._history[-(self._max_history_turns * 2) :]:
                        input_msgs.append(item)
                input_msgs.append({"role": "user", "content": turn_packet})
                kwargs["input"] = input_msgs

            resp = self.client.responses.create(**kwargs)
            text = self._clean_speech(self._extract_text(resp))
            if not text:
                raise ValueError("empty_openai_responses_output")

            response_id = str(getattr(resp, "id", "")).strip()
            self.previous_response_id = response_id or self.previous_response_id
            self._seeded = True
            self._history.extend(
                [
                    {"role": "user", "content": turn_packet},
                    {"role": "assistant", "content": text},
                ]
            )

            meta = {
                "agent_profile_id": self.agent_profile_id,
                "source": "openai_responses",
                "response_id": response_id,
                "fallback": False,
            }
            return text, meta
        except Exception as exc:
            text, _ = self._fallback_turn(call_taker_text=call_taker_text, system_events=system_events)
            return text, {
                "agent_profile_id": self.agent_profile_id,
                "source": "builtin_fallback",
                "fallback": True,
                "error_code": type(exc).__name__,
            }


class OpenAICallTakerAgent:
    def __init__(
        self,
        model: str,
        temperature: float,
        incident_json: dict[str, Any],
        agent_config: dict[str, Any] | None = None,
    ) -> None:
        cfg = agent_config or {}
        self.client = _create_openai_client()
        self.model = str(cfg.get("model", model))
        self.temperature = float(cfg.get("temperature", temperature))
        self.system_prompt = str(
            cfg.get(
                "system_prompt",
                "You are a 911 call-taker. Return strict JSON with keys: text(str), cad_updates(object), end_call(bool), end_reason(str|null).",
            )
        )
        self.incident_json = incident_json
        self.opening_greeting = str(
            cfg.get(
                "opening_greeting",
                "This is 911. Do you need Police, Fire or Ambulance?",
            )
        ).strip()
        self._opening_sent = False
        self._fallback = CallTakerAgent(incident_json=incident_json, temperature=temperature)

    def _fallback_decision(
        self,
        *,
        caller_text: str,
        cad_state: dict[str, Any],
        system_events: list[dict[str, Any]],
        reason: str,
        error_code: str | None = None,
    ) -> CTDecision:
        d = self._fallback.next_turn(caller_text=caller_text, cad_state=cad_state, system_events=system_events)
        md: dict[str, Any] = {
            "agent_profile_id": "openai_calltaker_json",
            "source": "builtin_fallback",
            "fallback": True,
            "fallback_reason": reason,
        }
        if error_code:
            md["error_code"] = error_code
        d.call_taker_metadata = md
        return d

    def next_turn(
        self,
        caller_text: str,
        cad_state: dict[str, Any],
        system_events: list[dict[str, Any]],
        pending_checkpoints: list[dict[str, Any]] | None = None,
    ) -> CTDecision:
        try:
            if not self._opening_sent and not str(caller_text or "").strip():
                self._opening_sent = True
                return CTDecision(
                    text=self.opening_greeting,
                    cad_updates={},
                    end_call=False,
                    call_taker_metadata={
                        "agent_profile_id": "openai_calltaker_json",
                        "source": "openai",
                        "fallback": False,
                    },
                )
            prompt = (
                f"caller_text={caller_text}\n"
                f"cad_state={json.dumps(cad_state)}\n"
                f"system_events={json.dumps(system_events[-3:])}\n"
                f"pending_checkpoints={json.dumps(pending_checkpoints or [])}\n"
            )
            resp = self.client.chat.completions.create(
                model=self.model,
                temperature=self.temperature,
                messages=[
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": prompt},
                ],
            )
            raw = (resp.choices[0].message.content or "").strip()
            obj = json.loads(raw)
            if not isinstance(obj, dict):
                raise ValueError("invalid_openai_ct_payload")
            return CTDecision(
                text=str(obj.get("text", "")) or "Acknowledged.",
                cad_updates=obj.get("cad_updates") if isinstance(obj.get("cad_updates"), dict) else {},
                end_call=bool(obj.get("end_call", False)),
                end_reason=str(obj.get("end_reason")) if obj.get("end_reason") else None,
                end_reason_detail=str(obj.get("end_reason_detail")) if obj.get("end_reason_detail") else None,
                checkpoint_decisions=(
                    obj.get("checkpoint_decisions") if isinstance(obj.get("checkpoint_decisions"), list) else []
                ),
                call_taker_metadata={
                    "agent_profile_id": "openai_calltaker_json",
                    "source": "openai",
                    "fallback": False,
                },
            )
        except Exception as exc:
            return self._fallback_decision(
                caller_text=caller_text,
                cad_state=cad_state,
                system_events=system_events,
                reason="exception",
                error_code=type(exc).__name__,
            )


class OpenAISyntheticCallTakerAgent:
    def __init__(
        self,
        model: str,
        temperature: float,
        incident_json: dict[str, Any],
        qa_template_json: dict[str, Any],
        agent_config: dict[str, Any] | None = None,
    ) -> None:
        cfg = agent_config or {}
        self.client = _create_openai_client()
        self.model = str(cfg.get("model", model))
        self.temperature = float(cfg.get("temperature", temperature))
        self.max_completion_tokens = int(cfg.get("max_completion_tokens", 500))
        self.use_previous_response_id = bool(cfg.get("use_previous_response_id", True))
        self.max_history_turns = int(cfg.get("max_history_turns", 20))
        self.enable_map_tool = bool(cfg.get("enable_map_tool", True))
        self.checkpoint_strategy = str(cfg.get("checkpoint_strategy", "llm_evaluate")).strip().lower()
        self.opening_greeting = str(
            cfg.get(
                "opening_greeting",
                "This is 911. Do you need Police, Fire or Ambulance?",
            )
        ).strip()
        self.system_prompt = str(
            cfg.get(
                "system_prompt",
                "You are a synthetic 911 call-taker agent. Use tools to gather SOP/CAD/QA/map context and decide CAD updates. "
                "Return strict JSON with keys text(str), cad_updates(object), end_call(bool), end_reason(str|null), "
                "end_reason_detail(str|null), checkpoint_decisions(array).",
            )
        )
        self.incident_json = incident_json
        self.qa_template_json = qa_template_json
        self._fallback = CallTakerAgent(incident_json=incident_json, temperature=temperature)
        self._pending_updates: dict[str, Any] = {}
        self._pending_end_call: dict[str, Any] = {}
        self._pending_checkpoint_decisions: list[dict[str, Any]] = []
        self._pending_checkpoints: list[dict[str, Any]] = []
        self._opening_sent = False
        self._seeded = False
        self.previous_response_id: str | None = None
        self._history: list[dict[str, str]] = []

    def _fallback_decision(
        self,
        *,
        caller_text: str,
        cad_state: dict[str, Any],
        system_events: list[dict[str, Any]],
        reason: str,
        error_code: str | None = None,
    ) -> CTDecision:
        d = self._fallback.next_turn(caller_text=caller_text, cad_state=cad_state, system_events=system_events)
        md: dict[str, Any] = {
            "agent_profile_id": "openai_synthetic_v1",
            "source": "builtin_fallback",
            "fallback": True,
            "fallback_reason": reason,
        }
        if error_code:
            md["error_code"] = error_code
        d.call_taker_metadata = md
        return d

    def next_turn(
        self,
        caller_text: str,
        cad_state: dict[str, Any],
        system_events: list[dict[str, Any]],
        pending_checkpoints: list[dict[str, Any]] | None = None,
    ) -> CTDecision:
        self._pending_updates = {}
        self._pending_end_call = {}
        self._pending_checkpoint_decisions = []
        self._pending_checkpoints = list(pending_checkpoints or [])
        try:
            if not self._opening_sent and not str(caller_text or "").strip():
                self._opening_sent = True
                return CTDecision(
                    text=self.opening_greeting,
                    cad_updates={},
                    end_call=False,
                    call_taker_metadata={
                        "agent_profile_id": "openai_synthetic_v1",
                        "source": "openai_synthetic",
                        "fallback": False,
                    },
                )
            if self._pending_checkpoints and self.checkpoint_strategy in {"auto_approve", "auto-deny", "auto_deny"}:
                auto_decision = "approved" if self.checkpoint_strategy == "auto_approve" else "denied"
                checkpoint_decisions = [
                    {"request_id": str(req.get("request_id", "")), "decision": auto_decision}
                    for req in self._pending_checkpoints
                    if str(req.get("request_id", "")).strip()
                ]
                return CTDecision(
                    text="Please stay on the line while I continue processing your emergency.",
                    cad_updates={},
                    checkpoint_decisions=checkpoint_decisions,
                    call_taker_metadata={
                        "agent_profile_id": "openai_synthetic_v1",
                        "source": "openai_synthetic",
                        "fallback": False,
                    },
                )
            turn_packet = self._turn_packet(caller_text, cad_state, system_events, self._pending_checkpoints)
            kwargs: dict[str, Any] = {
                "model": self.model,
                "temperature": self.temperature,
                "max_output_tokens": self.max_completion_tokens,
            }
            if self.use_previous_response_id and self._seeded and self.previous_response_id:
                kwargs["previous_response_id"] = self.previous_response_id
                kwargs["input"] = [{"role": "user", "content": turn_packet}]
            else:
                input_msgs: list[dict[str, Any]] = [
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": self._seed_packet()},
                ]
                if self._history:
                    input_msgs.extend(self._history[-(self.max_history_turns * 2) :])
                input_msgs.append({"role": "user", "content": turn_packet})
                kwargs["input"] = input_msgs

            resp = self.client.responses.create(**kwargs)
            response_id = str(getattr(resp, "id", "")).strip()
            self.previous_response_id = response_id or self.previous_response_id
            self._seeded = True
            raw_text = self._extract_text(resp)
            parsed = self._parse_ct_json(raw_text)
            text_out = str(parsed.get("text", "") or "Acknowledged.")
            self._history.extend(
                [
                    {"role": "user", "content": turn_packet},
                    {"role": "assistant", "content": text_out},
                ]
            )
            cad_updates = parsed.get("cad_updates") if isinstance(parsed.get("cad_updates"), dict) else {}
            merged_updates = dict(self._pending_updates)
            merged_updates.update(cad_updates)
            parsed_checkpoint_decisions = (
                parsed.get("checkpoint_decisions")
                if isinstance(parsed.get("checkpoint_decisions"), list)
                else []
            )
            merged_checkpoint_decisions = list(self._pending_checkpoint_decisions)
            merged_checkpoint_decisions.extend(parsed_checkpoint_decisions)
            end_call = bool(parsed.get("end_call", False) or self._pending_end_call.get("end_call", False))
            end_reason = parsed.get("end_reason") if parsed.get("end_reason") else self._pending_end_call.get("end_reason")
            end_reason_detail = (
                parsed.get("end_reason_detail")
                if parsed.get("end_reason_detail")
                else self._pending_end_call.get("end_reason_detail")
            )
            return CTDecision(
                text=text_out,
                cad_updates=merged_updates,
                end_call=end_call,
                end_reason=str(end_reason) if end_reason else None,
                end_reason_detail=str(end_reason_detail) if end_reason_detail else None,
                checkpoint_decisions=[d for d in merged_checkpoint_decisions if isinstance(d, dict)],
                call_taker_metadata={
                    "agent_profile_id": "openai_synthetic_v1",
                    "source": "openai_responses",
                    "response_id": response_id,
                    "fallback": False,
                },
            )
        except Exception as exc:
            return self._fallback_decision(
                caller_text=caller_text,
                cad_state=cad_state,
                system_events=system_events,
                reason="exception",
                error_code=type(exc).__name__,
            )

    def _seed_packet(self) -> str:
        payload = {
            "qa_template": self.qa_template_json,
            "tool_contract": {
                "write_cad": "Provide CAD updates in cad_updates object.",
                "end_call": "Set end_call=true and provide end_reason/end_reason_detail when call should close.",
                "checkpoints": "Return checkpoint_decisions array when pending checkpoints exist.",
            },
        }
        return (
            "Use this structured seed data for internal behavior only. "
            "Do not quote it to the caller.\n"
            f"seed_json={json.dumps(payload)}"
        )

    def _turn_packet(
        self,
        caller_text: str,
        cad_state: dict[str, Any],
        system_events: list[dict[str, Any]],
        pending_checkpoints: list[dict[str, Any]],
    ) -> str:
        payload = {
            "caller_text": caller_text,
            "cad_state": cad_state,
            "system_events": system_events[-8:],
            "pending_checkpoints": pending_checkpoints,
        }
        return json.dumps(payload)

    def _extract_text(self, resp: Any) -> str:
        output_text = str(getattr(resp, "output_text", "") or "").strip()
        if output_text:
            return output_text
        output = getattr(resp, "output", None)
        if isinstance(output, list):
            chunks: list[str] = []
            for item in output:
                content = getattr(item, "content", None)
                if isinstance(content, list):
                    for part in content:
                        txt = str(getattr(part, "text", "") or "").strip()
                        if txt:
                            chunks.append(txt)
            joined = "\n".join(chunks).strip()
            if joined:
                return joined
        return ""

    def _parse_ct_json(self, raw: str) -> dict[str, Any]:
        text = str(raw or "").strip()
        if not text:
            return {}
        try:
            obj = json.loads(text)
            return obj if isinstance(obj, dict) else {}
        except Exception:
            m = re.search(r"\{.*\}", text, flags=re.DOTALL)
            if not m:
                return {"text": text, "cad_updates": {}, "end_call": False}
            try:
                obj = json.loads(m.group(0))
                return obj if isinstance(obj, dict) else {}
            except Exception:
                return {"text": text, "cad_updates": {}, "end_call": False}

    def _tool_specs(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = [
            {
                "type": "function",
                "function": {
                    "name": "read_sop",
                    "description": "Read SOP snippets by incident_type and step.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "incident_type": {"type": "string"},
                            "step": {"type": "string"},
                        },
                        "required": [],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_cad_state",
                    "description": "Read the current CAD state snapshot.",
                    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_qa_template",
                    "description": "Read QA template sections/items.",
                    "parameters": {
                        "type": "object",
                        "properties": {"section": {"type": "string"}},
                        "required": [],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "write_cad",
                    "description": "Queue CAD field updates to apply this turn.",
                    "parameters": {
                        "type": "object",
                        "properties": {"updates": {"type": "object"}},
                        "required": ["updates"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "end_call",
                    "description": "Flag call termination.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "reason": {"type": "string"},
                            "reason_detail": {"type": "string"},
                        },
                        "required": ["reason"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_checkpoints",
                    "description": "List pending checkpoint requests for the call-taker role.",
                    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "submit_checkpoint",
                    "description": "Queue a checkpoint decision for submission.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "request_id": {"type": "string"},
                            "decision": {"type": "string"},
                            "edited_payload": {"type": "object"},
                            "rationale": {"type": "string"},
                            "re_escalate_to": {"type": "string"},
                        },
                        "required": ["request_id", "decision"],
                        "additionalProperties": False,
                    },
                },
            },
        ]
        if self.enable_map_tool:
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": "view_map",
                        "description": "Inspect approximate map/location context for the incident.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string"},
                            },
                            "required": [],
                            "additionalProperties": False,
                        },
                    },
                }
            )
        return tools

    def _exec_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
        cad_state: dict[str, Any],
        system_events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if tool_name == "read_sop":
            incident_type = str(args.get("incident_type") or self.incident_json.get("type", "Fire")).title()
            step = str(args.get("step", "initial")).lower()
            return {"snippets": self._sop_snippets(incident_type, step)}
        if tool_name == "read_cad_state":
            return {"cad_state": cad_state, "system_events_recent": system_events[-5:]}
        if tool_name == "read_qa_template":
            section = str(args.get("section", "")).strip().upper()
            templates = self.qa_template_json.get("templates", {})
            if section and isinstance(templates, dict):
                return {"section": section, "template": templates.get(section)}
            return {"templates": templates}
        if tool_name == "view_map":
            loc = self.incident_json.get("location", {}) if isinstance(self.incident_json.get("location"), dict) else {}
            return {
                "query": str(args.get("query", "")),
                "location": {
                    "address_line": loc.get("address_line"),
                    "city": loc.get("city"),
                    "lat": loc.get("lat"),
                    "lon": loc.get("lon"),
                    "accuracy_m": loc.get("accuracy_m"),
                },
            }
        if tool_name == "write_cad":
            updates = args.get("updates") if isinstance(args.get("updates"), dict) else {}
            self._pending_updates.update(updates)
            return {"queued_updates": updates}
        if tool_name == "end_call":
            reason = str(args.get("reason", "")).strip() or "other"
            detail = str(args.get("reason_detail", "")).strip() or None
            self._pending_end_call = {"end_call": True, "end_reason": reason, "end_reason_detail": detail}
            return {"queued_end_call": self._pending_end_call}
        if tool_name == "list_checkpoints":
            return {"pending_checkpoints": self._pending_checkpoints}
        if tool_name == "submit_checkpoint":
            req_id = str(args.get("request_id", "")).strip()
            decision = str(args.get("decision", "")).strip()
            if not req_id or not decision:
                return {"error": "request_id_and_decision_required"}
            decision_obj: dict[str, Any] = {"request_id": req_id, "decision": decision}
            if isinstance(args.get("edited_payload"), dict):
                decision_obj["edited_payload"] = args["edited_payload"]
            if args.get("rationale"):
                decision_obj["rationale"] = str(args["rationale"])
            if args.get("re_escalate_to"):
                decision_obj["re_escalate_to"] = str(args["re_escalate_to"])
            self._pending_checkpoint_decisions.append(decision_obj)
            return {"queued_checkpoint_decision": decision_obj}
        return {"error": f"unknown_tool:{tool_name}"}

    def _sop_snippets(self, incident_type: str, step: str) -> list[dict[str, str]]:
        by_type = {
            "Fire": [
                {"step": "initial", "title": "Fire Initial Triage", "text": "Confirm exact location, occupants, flame/smoke conditions, hazards."},
                {"step": "dispatch", "title": "Fire Dispatch Guidance", "text": "Dispatch immediately if active fire is confirmed; maintain line safety guidance."},
            ],
            "Police": [
                {"step": "initial", "title": "Police Initial Triage", "text": "Assess immediate threat, weapons, suspect description, scene safety."},
                {"step": "dispatch", "title": "Police Dispatch Guidance", "text": "Prioritize active violence and officer safety information."},
            ],
            "Ems": [
                {"step": "initial", "title": "EMS Initial Triage", "text": "Assess consciousness, breathing, bleeding, patient age/condition."},
                {"step": "dispatch", "title": "EMS Dispatch Guidance", "text": "Dispatch for life threats; provide immediate pre-arrival instructions."},
            ],
        }
        snippets = by_type.get(incident_type.title(), [])
        if step == "all":
            return snippets
        return [row for row in snippets if row.get("step") == step] or snippets[:1]


class OpenAIQAEvaluatorAgent:
    def __init__(
        self,
        model: str,
        temperature: float,
        qa_template_json: dict[str, Any],
        agent_config: dict[str, Any] | None = None,
    ) -> None:
        cfg = agent_config or {}
        self.client = _create_openai_client()
        self.model = str(cfg.get("model", model))
        self.temperature = float(cfg.get("temperature", temperature))
        self.max_output_tokens = int(cfg.get("max_output_tokens", 900))
        self.parse_retry_max = int(cfg.get("parse_retry_max", 2))
        self.use_responses_api = bool(cfg.get("use_responses_api", True))
        self.system_prompt = str(
            cfg.get(
                "system_prompt",
                "Score this 911 transcript. Return strict JSON with fields matching deterministic QA output.",
            )
        )
        self.qa_template_json = qa_template_json
        self._fallback = QAEvaluatorAgent(qa_template_json=qa_template_json, temperature=temperature)

    def evaluate(
        self,
        events: list[dict[str, Any]],
        incident_type: str,
        qa_input: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        packet = qa_input if isinstance(qa_input, dict) else self._build_packet(events=events, incident_type=incident_type)
        prompt = json.dumps(packet)
        last_error_code = "unknown"
        retries = 0
        for attempt in range(max(1, self.parse_retry_max + 1)):
            try:
                if self.use_responses_api:
                    resp = self.client.responses.create(
                        model=self.model,
                        temperature=self.temperature,
                        max_output_tokens=self.max_output_tokens,
                        input=[
                            {"role": "system", "content": self.system_prompt},
                            {"role": "user", "content": prompt},
                        ],
                    )
                    response_id = str(getattr(resp, "id", "")).strip()
                    raw = self._extract_text(resp)
                else:
                    resp = self.client.chat.completions.create(
                        model=self.model,
                        temperature=self.temperature,
                        messages=[
                            {"role": "system", "content": self.system_prompt},
                            {"role": "user", "content": prompt},
                        ],
                    )
                    response_id = ""
                    raw = str(resp.choices[0].message.content or "").strip()
                parsed = self._parse_json_payload(raw)
                normalized = self._normalize_score_payload(parsed, incident_type=incident_type, qa_input=packet)
                normalized["parse_retry_count"] = retries
                normalized["evaluator_model"] = self.model
                normalized["evaluator_source"] = "openai_responses" if self.use_responses_api else "openai_chat"
                normalized["evaluator_response_id"] = response_id
                normalized["fallback"] = False
                return normalized
            except Exception as exc:
                last_error_code = type(exc).__name__
                retries = attempt + 1
                continue
        fallback = self._fallback.evaluate(events=events, incident_type=incident_type)
        fallback["evaluator_source"] = "builtin_fallback"
        fallback["fallback"] = True
        fallback["error_code"] = last_error_code
        return fallback

    def _build_packet(self, *, events: list[dict[str, Any]], incident_type: str) -> dict[str, Any]:
        transcript = self._conversation_rows(events)
        return {
            "incident_type": incident_type,
            "template": self.qa_template_json,
            "transcript": transcript,
            "required_output_fields": [
                "evaluator_agent_id",
                "qa_template_id",
                "incident_type",
                "sections_applied",
                "items",
                "total_points_awarded",
                "total_points_possible",
                "normalized_score",
                "notes",
            ],
        }

    def _parse_json_payload(self, raw: str) -> dict[str, Any]:
        text = str(raw or "").strip()
        if not text:
            raise ValueError("empty_qa_output")
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
        m = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not m:
            raise ValueError("invalid_qa_json")
        obj = json.loads(m.group(0))
        if not isinstance(obj, dict):
            raise ValueError("invalid_qa_json_object")
        return obj

    def _normalize_score_payload(self, obj: dict[str, Any], incident_type: str, qa_input: dict[str, Any] | None = None) -> dict[str, Any]:
        if "normalized_score" not in obj:
            raise ValueError("qa_missing_normalized_score")
        template_idx = self._template_item_index(str(incident_type).upper())
        transcript = (qa_input or {}).get("transcript", []) if isinstance(qa_input, dict) else []
        items = obj.get("items") if isinstance(obj.get("items"), list) else []
        if not items and isinstance(obj.get("rows"), list):
            items = obj.get("rows")
        norm_items: list[dict[str, Any]] = []
        answer_enum = {"YES", "NO", "REFUSED", "NA"}
        awarded = 0.0
        possible = 0.0
        for it in items:
            if not isinstance(it, dict):
                continue
            item_id = str(it.get("id", "unknown"))
            tpl = template_idx.get(item_id, {})
            pp_raw = it.get("points_possible", it.get("max_points", tpl.get("points", 0.0)))
            pa_raw = it.get("points_awarded", it.get("awarded", 0.0))
            pp = float(pp_raw or 0.0)
            pa = float(pa_raw or 0.0)
            ans = str(it.get("answer", "")).strip().upper()
            if ans not in answer_enum:
                if pp <= 0:
                    ans = "NA"
                elif pa <= 0:
                    ans = "NO"
                else:
                    ans = "YES"
            # Policy hardening: if call-taker clearly asked the required question,
            # award full credit even when caller refused/couldn't answer.
            if ans in {"REFUSED", "NO"} and pp > 0 and self._was_required_question_clearly_asked(
                item_id=item_id,
                template_row=tpl,
                transcript=transcript,
            ):
                ans = "YES"
            # Resolve inconsistent model outputs deterministically:
            # if positive points are awarded, treat as YES regardless of answer token.
            if pp > 0 and pa > 0 and ans != "YES":
                ans = "YES"
            # Enforce binary/no-partial scoring policy:
            # YES => full points; NO/REFUSED/NA => 0
            pa = pp if ans == "YES" else 0.0
            awarded += pa
            # NA items are excluded from normalization denominator.
            possible += 0.0 if ans == "NA" else pp
            norm_items.append(
                {
                    "id": item_id,
                    "answer": ans,
                    "points_awarded": pa,
                    "points_possible": pp,
                    "rationale": str(it.get("rationale", "")),
                    "evidence_turns": [int(x) for x in it.get("evidence_turns", []) if isinstance(x, (int, float))],
                }
            )
        total_awarded = awarded
        total_possible = possible
        normalize_to = float(self.qa_template_json.get("normalize_to", 100) or 100.0)
        normalized = (total_awarded / total_possible * normalize_to) if total_possible > 0 else 0.0
        qa_template_id = str(self.qa_template_json.get("version", obj.get("qa_template_id", "unknown")))
        sections = obj.get("sections_applied")
        if not isinstance(sections, list):
            sections = ["COMMON", str(incident_type).upper()]
        return {
            "evaluator_agent_id": str(obj.get("evaluator_agent_id", "qa-agent")),
            "qa_template_id": qa_template_id,
            "incident_type": str(obj.get("incident_type", incident_type)),
            "sections_applied": [str(s) for s in sections],
            "items": norm_items,
            "total_points_awarded": total_awarded,
            "total_points_possible": total_possible,
            "normalized_score": max(0.0, min(100.0, normalized)),
            "parse_retry_count": 0,
            "notes": str(obj.get("notes", "")).strip(),
        }

    def _was_required_question_clearly_asked(
        self,
        *,
        item_id: str,
        template_row: dict[str, Any],
        transcript: Any,
    ) -> bool:
        if not isinstance(transcript, list) or not transcript:
            return False
        ct_lines = [str(r.get("call_taker", "")).lower() for r in transcript if isinstance(r, dict)]
        if not ct_lines:
            return False

        q = str(template_row.get("question", "")).strip().lower()
        if not q:
            return False

        # Fast path for common interview fields.
        special_patterns: list[tuple[list[str], list[str]]] = [
            (["name"], ["name", "who am i speaking", "your name"]),
            (["location", "address"], ["where", "address", "location", "what is the address"]),
            (["phone", "callback", "number"], ["phone", "number", "callback", "call back"]),
            (["people", "involved"], ["who is involved", "anyone else", "how many people"]),
            (["relationship"], ["relationship", "do you know", "how do you know"]),
        ]
        for q_terms, prompts in special_patterns:
            if any(term in q for term in q_terms):
                return any(any(p in line for p in prompts) for line in ct_lines)

        # Generic lexical match: require at least 2 meaningful question tokens in a single line.
        stop = {
            "the", "and", "for", "are", "you", "your", "with", "what", "when", "where",
            "which", "that", "this", "from", "have", "has", "been", "was", "were", "can",
            "could", "would", "should", "please", "tell", "about", "there", "they", "them",
            "is", "it", "to", "of", "in", "on", "a", "an",
        }
        tokens = [t for t in re.findall(r"[a-z0-9]+", q) if len(t) >= 4 and t not in stop]
        if len(tokens) < 2:
            return False
        for line in ct_lines:
            matches = sum(1 for t in set(tokens) if t in line)
            if matches >= 2:
                return True
        return False

    def _conversation_rows(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for ev in events:
            if ev.get("event_type") != "conversation":
                continue
            rows.append(
                {
                    "turn": int(ev.get("turn", 0) or 0),
                    "call_taker": str(ev.get("call_taker", "")),
                    "caller": str(ev.get("caller", "")),
                }
            )
        return rows

    def _template_item_index(self, incident_type: str) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        templates = self.qa_template_json.get("templates", {})
        if not isinstance(templates, dict):
            return out
        for key in ("COMMON", incident_type):
            block = templates.get(key)
            if not isinstance(block, dict):
                continue
            for sec in block.get("sections", []):
                if not isinstance(sec, dict):
                    continue
                for item in sec.get("items", []):
                    if isinstance(item, dict) and item.get("id"):
                        out[str(item["id"])] = {
                            "points": float(item.get("points", 0.0) or 0.0),
                            "question": str(item.get("question", "")),
                        }
        return out

    def _extract_text(self, resp: Any) -> str:
        output_text = str(getattr(resp, "output_text", "") or "").strip()
        if output_text:
            return output_text
        output = getattr(resp, "output", None)
        if isinstance(output, list):
            chunks: list[str] = []
            for item in output:
                content = getattr(item, "content", None)
                if isinstance(content, list):
                    for part in content:
                        txt = str(getattr(part, "text", "") or "").strip()
                        if txt:
                            chunks.append(txt)
            joined = "\n".join(chunks).strip()
            if joined:
                return joined
        return ""


def _create_openai_caller(
    profile: AgentProfile,
    caller_json: dict[str, Any],
    incident_json: dict[str, Any],
    *,
    agent_config: dict[str, Any],
) -> OpenAICallerAgent:
    return OpenAICallerAgent(
        model=profile.model,
        temperature=profile.temperature,
        caller_json=caller_json,
        incident_json=incident_json,
        agent_config=agent_config,
        agent_profile_id=profile.id,
    )


def _create_openai_calltaker(profile: AgentProfile, incident_json: dict[str, Any], *, agent_config: dict[str, Any]) -> OpenAICallTakerAgent:
    return OpenAICallTakerAgent(
        model=profile.model,
        temperature=profile.temperature,
        incident_json=incident_json,
        agent_config=agent_config,
    )


def _create_openai_synthetic_calltaker(
    profile: AgentProfile,
    incident_json: dict[str, Any],
    *,
    qa_template_json: dict[str, Any],
    agent_config: dict[str, Any],
) -> OpenAISyntheticCallTakerAgent:
    return OpenAISyntheticCallTakerAgent(
        model=profile.model,
        temperature=profile.temperature,
        incident_json=incident_json,
        qa_template_json=qa_template_json,
        agent_config=agent_config,
    )


def _create_openai_qa(profile: AgentProfile, qa_template_json: dict[str, Any], *, agent_config: dict[str, Any]) -> OpenAIQAEvaluatorAgent:
    return OpenAIQAEvaluatorAgent(
        model=profile.model,
        temperature=profile.temperature,
        qa_template_json=qa_template_json,
        agent_config=agent_config,
    )
