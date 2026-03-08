"""Role-based agent provider registry for deterministic/replay/OpenAI agents."""

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


_CATALOG: list[AgentProfile] = [
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
        id="openai_gpt4o_mini_v1",
        role="caller",
        provider="openai",
        model="gpt-4o-mini",
        temperature=0.3,
        description="OpenAI caller adapter (Responses API)",
        mode="callable",
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
        id="openai_gpt4o_mini_v1",
        role="calltaker",
        provider="openai",
        model="gpt-4o-mini",
        temperature=0.0,
        description="OpenAI call-taker adapter (JSON action output)",
        mode="callable",
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
    AgentProfile(
        id="openai_gpt4o_mini_v1",
        role="qa",
        provider="openai",
        model="gpt-4o-mini",
        temperature=0.0,
        description="OpenAI QA adapter (JSON rubric scoring)",
        mode="callable",
    ),
]


def list_profiles() -> list[dict[str, Any]]:
    return [asdict(p) for p in _CATALOG]


def get_profile(role: str, agent_id: str) -> AgentProfile:
    for p in _CATALOG:
        if p.role == role and p.id == agent_id:
            return p
    raise ValueError(f"unknown agent profile: role={role}, id={agent_id}")


def is_replay(role: str, agent_id: str) -> bool:
    return get_profile(role, agent_id).mode == "replay"


def is_manual(role: str, agent_id: str) -> bool:
    return get_profile(role, agent_id).mode == "manual"


def create_caller_agent(
    agent_id: str,
    caller_json: dict[str, Any],
    incident_json: dict[str, Any],
    *,
    config_root: str | Path | None = None,
) -> CallerAgent | Any | None:
    profile = get_profile("caller", agent_id)
    if profile.mode in {"manual", "replay"}:
        return None
    if profile.provider == "builtin":
        return CallerAgent(caller_json=caller_json, incident_json=incident_json, temperature=profile.temperature)
    cfg = _load_agent_config(config_root=config_root, role="caller", agent_id=agent_id)
    return _create_openai_caller(profile, caller_json, incident_json, agent_config=cfg)


def create_calltaker_agent(
    agent_id: str,
    incident_json: dict[str, Any],
    *,
    config_root: str | Path | None = None,
    **kwargs: Any,
) -> CallTakerAgent | Any | None:
    profile = get_profile("calltaker", agent_id)
    if profile.mode in {"manual", "replay"}:
        return None
    if profile.provider == "builtin":
        return CallTakerAgent(incident_json=incident_json, temperature=profile.temperature, **kwargs)
    cfg = _load_agent_config(config_root=config_root, role="calltaker", agent_id=agent_id)
    return _create_openai_calltaker(profile, incident_json, agent_config=cfg)


def create_qa_agent(
    agent_id: str,
    qa_template_json: dict[str, Any],
    *,
    config_root: str | Path | None = None,
    **kwargs: Any,
) -> QAEvaluatorAgent | Any | None:
    profile = get_profile("qa", agent_id)
    if profile.mode in {"manual", "replay"}:
        return None
    if profile.provider == "builtin":
        return QAEvaluatorAgent(qa_template_json=qa_template_json, temperature=profile.temperature, **kwargs)
    cfg = _load_agent_config(config_root=config_root, role="qa", agent_id=agent_id)
    return _create_openai_qa(profile, qa_template_json, agent_config=cfg)


def _load_agent_config(config_root: str | Path | None, role: str, agent_id: str) -> dict[str, Any]:
    if config_root is None:
        return {}
    base = Path(config_root)
    path = base / f"{role}.{agent_id}.yaml"
    if not path.exists():
        return {}
    try:
        import yaml  # type: ignore
    except Exception:
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return raw if isinstance(raw, dict) else {}


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
        self._fallback = CallTakerAgent(incident_json=incident_json, temperature=temperature)

    def next_turn(self, caller_text: str, cad_state: dict[str, Any], system_events: list[dict[str, Any]]) -> CTDecision:
        try:
            prompt = (
                f"caller_text={caller_text}\n"
                f"cad_state={json.dumps(cad_state)}\n"
                f"system_events={json.dumps(system_events[-3:])}\n"
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
            )
        except Exception:
            return self._fallback.next_turn(caller_text=caller_text, cad_state=cad_state, system_events=system_events)


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
        self.system_prompt = str(
            cfg.get(
                "system_prompt",
                "Score this 911 transcript. Return strict JSON with fields matching deterministic QA output.",
            )
        )
        self.qa_template_json = qa_template_json
        self._fallback = QAEvaluatorAgent(qa_template_json=qa_template_json, temperature=temperature)

    def evaluate(self, events: list[dict[str, Any]], incident_type: str) -> dict[str, Any]:
        try:
            prompt = f"incident_type={incident_type}\nevents={json.dumps(events[-30:])}\n"
            resp = self.client.chat.completions.create(
                model=self.model,
                temperature=self.temperature,
                messages=[
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": prompt},
                ],
            )
            raw = (resp.choices[0].message.content or "").strip()
            parsed = json.loads(raw)
            if not isinstance(parsed, dict) or "normalized_score" not in parsed:
                raise ValueError("invalid_openai_qa_payload")
            return parsed
        except Exception:
            return self._fallback.evaluate(events=events, incident_type=incident_type)


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


def _create_openai_qa(profile: AgentProfile, qa_template_json: dict[str, Any], *, agent_config: dict[str, Any]) -> OpenAIQAEvaluatorAgent:
    return OpenAIQAEvaluatorAgent(
        model=profile.model,
        temperature=profile.temperature,
        qa_template_json=qa_template_json,
        agent_config=agent_config,
    )
