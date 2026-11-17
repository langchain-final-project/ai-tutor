# 합성 등식으로 서비스 진입점의 새 검증·고정 출력·입력 보호를 확인함
# 저장소 루트: python3 -B -m unittest discover -s backend/tests -p 'test_solution_service.py'
# 모델·서버를 호출하지 않으며 사진 인식·승인 화면·서비스 통합 성능은 검증하지 않음.

import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.tutor import evaluate_reviewed_solution


def inputs(equation="2*(x+3)=10", lines=("x=2",)):
    problem = {
        "schema_version": "verification-problem-v1", "id": "service-example",
        "revision": "1", "equation": equation, "variable": "x", "domain": "real",
        "confirmed": True, "profile": "linear-equation-v1",
    }
    work = {
        "schema_version": "verification-work-v1", "problem_id": problem["id"],
        "problem_revision": problem["revision"],
        "steps": [{"id": f"s{i}", "text": line, "state": "active", "confirmed": True}
                  for i, line in enumerate(lines, 1)],
    }
    return problem, work


class SolutionServiceTests(unittest.TestCase):
    def test_completed_work_returns_fixed_feedback_without_model_context(self):
        result = evaluate_reviewed_solution(*inputs())
        self.assertEqual(result["verification"]["judgement"], "correct")
        self.assertEqual(result["student_feedback"]["completion"], "complete")
        self.assertFalse(result["model_route"]["model_required"])
        self.assertEqual(result["model_route"]["reason"], "completed_correct")
        self.assertIsNone(result["model_context"])
        json.dumps(result, allow_nan=False)

    def test_problem_change_same_revision_recomputes_instead_of_reusing_verdict(self):
        p, w = inputs("x=2")
        original = evaluate_reviewed_solution(p, w)
        p["equation"] = "x=3"
        updated = evaluate_reviewed_solution(p, w)
        self.assertEqual(original["verification"]["judgement"], "correct")
        self.assertEqual(updated["verification"]["judgement"], "incorrect")
        self.assertEqual(updated["verification"]["first_error_step_id"], "s1")
        self.assertTrue(updated["model_route"]["model_required"])
        self.assertNotEqual(original["input_fingerprint"], updated["input_fingerprint"])

    def test_confirmation_withdrawal_never_keeps_a_completed_route(self):
        for owner in ("problem", "step"):
            with self.subTest(owner=owner):
                p, w = inputs()
                evaluate_reviewed_solution(p, w)
                target = p if owner == "problem" else w["steps"][0]
                target["confirmed"] = False
                current = evaluate_reviewed_solution(p, w)
                self.assertEqual(current["verification"]["judgement"], "undetermined")
                self.assertEqual(current["verification"]["action"], "ask_confirmation")
                self.assertEqual(current["model_route"]["reason"], "needs_input_confirmation")
                self.assertIsNone(current["model_context"])

    def test_error_then_right_answer_retains_error_and_grounded_template(self):
        result = evaluate_reviewed_solution(*inputs(lines=("2*x+3=10", "x=2")))
        self.assertEqual(result["verification"]["completion"], "complete")
        self.assertEqual(result["verification"]["judgement"], "incorrect")
        self.assertEqual(result["error_evidence"]["target_side"], "lhs")
        self.assertEqual(result["student_feedback"]["first_error_step_id"], "s1")
        self.assertIn("왼쪽", result["student_feedback"]["hint"])
        self.assertTrue(result["model_route"]["model_required"])

    def test_problem_only_produces_first_hint_plan_and_fixed_fallback(self):
        result = evaluate_reviewed_solution(*inputs(lines=()))
        self.assertEqual(result["verification"]["completion"], "no_work")
        self.assertEqual(result["student_feedback"]["hint_template_id"], "first_hint")
        self.assertEqual(result["model_route"]["reason"], "first_hint_requested")
        self.assertIsNotNone(result["model_context"])

    def test_unresolved_prefix_does_not_claim_first_error_location(self):
        p, w = inputs(lines=("?", "x=4"))
        w["steps"][0].update(state="uncertain", confirmed=False)
        result = evaluate_reviewed_solution(p, w)
        self.assertEqual(result["verification"]["first_observed_error_step_id"], "s2")
        self.assertIsNone(result["verification"]["first_error_step_id"])
        self.assertEqual(result["model_route"]["reason"], "needs_input_confirmation")

    def test_unsupported_profile_is_not_a_student_error(self):
        p, w = inputs()
        p["profile"] = "geometry-v1"
        result = evaluate_reviewed_solution(p, w)
        self.assertEqual(result["verification"]["action"], "unsupported")
        self.assertEqual(result["verification"]["judgement"], "undetermined")
        self.assertEqual(result["model_route"]["reason"], "unsupported_scope")
        self.assertIsNone(result["model_context"])

    def test_mismatched_revision_requests_confirmation(self):
        p, w = inputs()
        p["revision"] = "2"
        result = evaluate_reviewed_solution(p, w)
        self.assertEqual(result["verification"]["reason_code"], "problem_revision_mismatch")
        self.assertEqual(result["model_route"]["reason"], "needs_input_confirmation")

    def test_inputs_and_previous_output_are_not_mutated_or_reused(self):
        p, w = inputs()
        before = copy.deepcopy((p, w))
        first = evaluate_reviewed_solution(p, w)
        self.assertEqual((p, w), before)
        first["student_feedback"]["judgement"] = "incorrect"
        first["verification"]["steps"].clear()
        second = evaluate_reviewed_solution(p, w)
        self.assertEqual(second["student_feedback"]["judgement"], "correct")
        self.assertEqual(len(second["verification"]["steps"]), 1)

    def test_external_result_and_unknown_input_fields_are_rejected(self):
        p, w = inputs()
        with self.assertRaises(TypeError):
            evaluate_reviewed_solution(p, w, result={"judgement": "correct"})
        p["model_judgement"] = "correct"
        with self.assertRaises(ValueError):
            evaluate_reviewed_solution(p, w)

    def test_context_mode_cannot_bypass_validation_on_skipped_requests(self):
        with self.assertRaises(ValueError):
            evaluate_reviewed_solution(*inputs(), context_mode="invalid")
        result = evaluate_reviewed_solution(*inputs(lines=("x+3=5",)), context_mode="full")
        self.assertEqual(result["model_route"]["reason"], "unfinished_work")
        self.assertIn("error_evidence", result["model_context"])


if __name__ == "__main__":
    unittest.main()
