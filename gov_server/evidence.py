"""Evidence validation for ActionProposals."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .types import JSONObject


@dataclass(frozen=True)
class EvidenceError:
    code: str
    detail: str


class EvidenceValidator:
    def __init__(self, evidence_config: JSONObject) -> None:
        self.subtypes: dict[str, JSONObject] = {
            item["name"]: item for item in evidence_config.get("subtypes", [])
        }

    def validate(
        self,
        proposal: JSONObject,
        action_registry_entry: JSONObject,
        context_snapshot: JSONObject,
    ) -> list[EvidenceError]:
        errors: list[EvidenceError] = []
        refs = proposal.get("evidence_refs", [])
        if not refs:
            errors.append(EvidenceError("missing_required_evidence", "no evidence_refs"))
            return errors

        required_categories = set(action_registry_entry.get("required_evidence", []))
        present_categories: set[str] = set()

        transcript_turns = set(context_snapshot.get("transcript_turns", []))
        sop_ids = set(context_snapshot.get("sop_ids", []))
        authorized_data_sources = set(action_registry_entry.get("authorized_data_sources", []))

        for ref in refs:
            subtype_name = ref.get("type")
            subtype = self.subtypes.get(subtype_name)
            if subtype is None:
                errors.append(EvidenceError("unknown_evidence_type", f"type={subtype_name}"))
                continue

            category = subtype.get("category")
            if ref.get("category") != category:
                errors.append(EvidenceError("evidence_category_mismatch", f"{subtype_name}:{ref.get('category')}!= {category}"))
            present_categories.add(category)

            content = ref.get("content")
            if not isinstance(content, str) or not content.strip():
                errors.append(EvidenceError("empty_evidence_content", f"type={subtype_name}"))

            confidence = ref.get("confidence")
            if confidence is not None and (not isinstance(confidence, (int, float)) or confidence < 0 or confidence > 1):
                errors.append(EvidenceError("invalid_confidence", f"type={subtype_name}"))
            if confidence is None and category != "procedural_reference":
                errors.append(EvidenceError("invalid_confidence", f"null_not_allowed:{subtype_name}"))

            source = str(ref.get("source", ""))
            if subtype_name in {"transcript_span", "entity_extract"}:
                if not self._turn_source_exists(source, transcript_turns):
                    errors.append(EvidenceError("invalid_evidence_source", f"source={source}"))
            elif subtype_name == "sop_ref":
                if not source.startswith("sop:") or source.split(":", 1)[1] not in sop_ids:
                    errors.append(EvidenceError("invalid_evidence_source", f"source={source}"))
            elif category == "external_source":
                lookup_service = ref.get("lookup_service")
                if lookup_service and authorized_data_sources and lookup_service not in authorized_data_sources:
                    errors.append(EvidenceError("unauthorized_data_source", f"service={lookup_service}"))

        missing = sorted(required_categories - present_categories)
        if missing:
            errors.append(EvidenceError("missing_required_evidence", ",".join(missing)))
        return errors

    @staticmethod
    def _turn_source_exists(source: str, transcript_turns: set[int]) -> bool:
        match = re.fullmatch(r"turn:(\d+)(?:-(\d+))?", source)
        if not match:
            return False
        start = int(match.group(1))
        end = int(match.group(2) or start)
        return all(turn in transcript_turns for turn in range(start, end + 1))
