"""Regression tests for asked-question full-credit QA normalization rule."""

from __future__ import annotations

import unittest

from agents.provider import OpenAIQAEvaluatorAgent


def _make_agent() -> OpenAIQAEvaluatorAgent:
    # Avoid network/client init; we only exercise normalization helpers.
    agent = object.__new__(OpenAIQAEvaluatorAgent)
    agent.qa_template_json = {
        "version": "003",
        "normalize_to": 100,
        "templates": {
            "COMMON": {
                "sections": [
                    {
                        "name": "Interview Basics",
                        "items": [
                            {"id": "ASK_LOCATION", "question": "What is the exact address?", "points": 4},
                            {"id": "ASK_NAME", "question": "What is your name?", "points": 2},
                        ],
                    }
                ]
            },
            "FIRE": {"sections": []},
        },
    }
    return agent


class TestQAAaskedQuestionRule(unittest.TestCase):
    def test_refused_gets_full_credit_when_question_clearly_asked(self) -> None:
        agent = _make_agent()
        model_obj = {
            "normalized_score": 0,
            "incident_type": "FIRE",
            "items": [
                {"id": "ASK_LOCATION", "answer": "REFUSED", "points_awarded": 0, "points_possible": 4, "rationale": "caller refused"},
            ],
        }
        qa_input = {
            "transcript": [
                {"turn": 1, "call_taker": "What is the exact address of the emergency?", "caller": "I won't tell you."},
            ]
        }
        out = agent._normalize_score_payload(model_obj, incident_type="FIRE", qa_input=qa_input)
        self.assertEqual(out["items"][0]["answer"], "YES")
        self.assertEqual(out["items"][0]["points_awarded"], 4.0)

    def test_no_without_clear_question_stays_zero(self) -> None:
        agent = _make_agent()
        model_obj = {
            "normalized_score": 0,
            "incident_type": "FIRE",
            "items": [
                {"id": "ASK_NAME", "answer": "NO", "points_awarded": 0, "points_possible": 2, "rationale": "not asked"},
            ],
        }
        qa_input = {
            "transcript": [
                {"turn": 1, "call_taker": "Stay calm and tell me what happened.", "caller": "There is smoke everywhere."},
            ]
        }
        out = agent._normalize_score_payload(model_obj, incident_type="FIRE", qa_input=qa_input)
        self.assertEqual(out["items"][0]["answer"], "NO")
        self.assertEqual(out["items"][0]["points_awarded"], 0.0)

    def test_question_detection_works_for_name_prompt_variation(self) -> None:
        agent = _make_agent()
        model_obj = {
            "normalized_score": 0,
            "incident_type": "FIRE",
            "items": [
                {"id": "ASK_NAME", "answer": "REFUSED", "points_awarded": 0, "points_possible": 2, "rationale": "refused"},
            ],
        }
        qa_input = {
            "transcript": [
                {"turn": 2, "call_taker": "Who am I speaking with today?", "caller": "I don't want to say."},
            ]
        }
        out = agent._normalize_score_payload(model_obj, incident_type="FIRE", qa_input=qa_input)
        self.assertEqual(out["items"][0]["answer"], "YES")
        self.assertEqual(out["items"][0]["points_awarded"], 2.0)

    def test_na_excluded_from_normalized_denominator(self) -> None:
        agent = _make_agent()
        model_obj = {
            "normalized_score": 0,
            "incident_type": "FIRE",
            "items": [
                {"id": "ASK_LOCATION", "answer": "YES", "points_awarded": 4, "points_possible": 4, "rationale": "asked"},
                {"id": "ASK_NAME", "answer": "NA", "points_awarded": 0, "points_possible": 2, "rationale": "not applicable"},
            ],
        }
        out = agent._normalize_score_payload(model_obj, incident_type="FIRE", qa_input={"transcript": []})
        self.assertEqual(out["total_points_awarded"], 4.0)
        # NA item excluded from denominator; expected possible remains 4.0, not 6.0.
        self.assertEqual(out["total_points_possible"], 4.0)
        self.assertEqual(out["normalized_score"], 100.0)


if __name__ == "__main__":
    unittest.main()
