# 합성 등식으로 고정 안내·오류 근거·입력 연결의 회귀 사례를 검증함
# 저장소 루트: python3 -B -m unittest discover -s backend/tests -p 'test_service_feedback.py'
# 모델 호출 없이 실행하며 OCR 정확도·자유 힌트 품질·학습 효과는 검증하지 않음

import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.tutor import feedback_policy as policy
from app.tutor import guided_feedback as guided
from app.tutor.step_verifier import verify


def pair(equation, lines, states=None, confirmed=None):
    problem = {
        "schema_version": "verification-problem-v1", "id": "service-test",
        "revision": "v1", "equation": equation, "variable": "x",
        "domain": "real", "confirmed": True, "profile": "linear-equation-v1",
    }
    work = {
        "schema_version": "verification-work-v1", "problem_id": problem["id"],
        "problem_revision": problem["revision"], "steps": [
            {"id": "s" + str(i + 1), "text": text,
             "state": states[i] if states is not None else "active",
             "confirmed": confirmed[i] if confirmed is not None else True}
            for i, text in enumerate(lines)
        ],
    }
    return problem, work, verify(problem, work)


class ServiceFeedbackTests(unittest.TestCase):
    def test_local_target_is_expression_change_not_inferred_operation(self):
        cases = [
            ("2*(x+2)=3*(x-1)", ["2*(x+2)=3*(x-1)", "2*x+2=3*x-3"], "lhs", "왼쪽"),
            ("2*x+4=3*(x-1)", ["2*x+4=3*(x-1)", "2*x+4=3*x-1"], "rhs", "오른쪽"),
        ]
        for equation, lines, side, word in cases:
            with self.subTest(side=side):
                problem, work, result = pair(equation, lines)
                evidence = guided.error_evidence(problem, work, result)
                self.assertEqual(evidence["target_side"], side)
                self.assertEqual(evidence["step_id"], "s2")
                self.assertEqual(evidence["previous_step_id"], "s1")
                self.assertFalse(evidence["evidence_basis"]["operation_intent_inferred"])
                feedback = guided.student_feedback(problem, work, result)
                self.assertEqual(feedback["first_error_step_id"], "s2")
                self.assertIn(word, feedback["hint"])

    def test_both_sides_changed_keeps_whole_equation_recheck(self):
        problem, work, result = pair(
            ".2*x=.75*x+1.2", [".2*x=.75*x+1.2", "4*x=15*x+12"])
        evidence = guided.error_evidence(problem, work, result)
        self.assertEqual(evidence["target_side"], "equation")
        self.assertEqual(evidence["reason_code"], "both_sides_changed_no_local_proof")
        self.assertFalse(evidence["evidence_basis"]["operation_intent_inferred"])
        self.assertIn("양변", guided.student_feedback(problem, work, result)["hint"])

    def test_first_observation_uses_problem_without_inventing_student_line(self):
        problem, work, result = pair("2*(x-4)+6=-12", ["2*x-4+6=-12"])
        evidence = guided.error_evidence(problem, work, result)
        self.assertEqual(evidence["target_side"], "lhs")
        self.assertIsNone(evidence["previous_step_id"])
        self.assertEqual(evidence["evidence_basis"]["origin"], "confirmed_problem")

    def test_correct_alternative_skipped_steps_complete_without_model_plan(self):
        problem, work, result = pair("2*(x+2)=3*(x-1)",
                                     ["2*(x+2)=3*(x-1)", "2*x+7=3*x", "7=x"])
        self.assertEqual(result["judgement"], "correct")
        self.assertEqual(result["completion"], "complete")
        self.assertEqual(guided.route(result)["reason"], "completed_correct")
        self.assertFalse(guided.route(result)["model_required"])
        self.assertIsNone(guided.error_evidence(problem, work, result)["step_id"])
        feedback = guided.student_feedback(problem, work, result)
        self.assertEqual(feedback["hint_template_id"], "verify_submitted")

    def test_correct_final_answer_does_not_erase_middle_error(self):
        """완료 상태와 풀이 이력의 정오 판정은 독립적이다."""
        problem, work, result = pair("2*(x+3)=10", ["2*(x+3)=10", "2*x+3=10", "x=2"])
        self.assertEqual(result["completion"], "complete")
        self.assertEqual(result["judgement"], "incorrect")
        self.assertEqual(guided.route(result)["reason"], "observed_error")
        self.assertTrue(guided.route(result)["model_required"])
        self.assertEqual(guided.student_feedback(problem, work, result)["first_error_step_id"], "s2")

    def test_unknown_prefix_does_not_claim_first_error_or_local_cause(self):
        """관찰된 오류와 최초 오류라고 확정할 수 있는 위치는 다르다."""
        problem, work, result = pair("2*(x+2)=3*(x-1)", ["?", "2*x+2=3*x-3"],
                                     states=["uncertain", "active"], confirmed=[False, True])
        evidence = guided.error_evidence(problem, work, result)
        self.assertEqual(evidence["target_side"], "unknown")
        self.assertIsNone(evidence["step_id"])
        feedback = guided.student_feedback(problem, work, result)
        self.assertIsNone(feedback["first_error_step_id"])
        self.assertEqual(feedback["first_observed_error_step_id"], "s2")
        self.assertEqual(feedback["earliest_unresolved_step_id"], "s1")
        self.assertEqual(feedback["hint_template_id"], "clarify")
        self.assertEqual(guided.route(result)["reason"], "needs_input_confirmation")
        self.assertFalse(guided.route(result)["model_required"])

    def test_only_confirmed_deleted_lines_are_ignored(self):
        for confirmed, reason in [([True, True], "completed_correct"),
                                  ([False, True], "needs_input_confirmation")]:
            with self.subTest(confirmed=confirmed):
                problem, work, result = pair("2*(x+3)=10", ["2*x+3=10", "x=2"],
                                             states=["deleted", "active"], confirmed=confirmed)
                self.assertEqual(guided.route(result)["reason"], reason)
                self.assertIsNone(guided.student_feedback(problem, work, result)["first_error_step_id"])

    def test_first_hint_unfinished_and_unsupported_stay_distinct(self):
        cases = [
            ("2*(x+3)=10", [], "first_hint_requested", "first_hint", True),
            ("2*(x+3)=10", ["x+3=5"], "unfinished_work", "continue", True),
            ("x*x=4", ["x=2"], "unsupported_scope", "unsupported", False),
            ("x=x", [], "unsupported_scope", "unsupported", False),
            ("x=x+1", [], "unsupported_scope", "unsupported", False),
        ]
        for equation, lines, reason, template, required in cases:
            with self.subTest(equation=equation, lines=lines):
                problem, work, result = pair(equation, lines)
                route = guided.route(result)
                self.assertEqual(route["reason"], reason)
                self.assertEqual(route["model_required"], required)
                feedback = guided.student_feedback(problem, work, result)
                self.assertEqual(feedback["hint_template_id"], template)

    def test_stale_result_rejected_after_equation_or_approval_change(self):
        problem, work, result = pair("x=2", ["x=2"])
        changed_equation = dict(problem, equation="x=3")
        unconfirmed_problem = dict(problem, confirmed=False)
        unconfirmed_work = copy.deepcopy(work)
        unconfirmed_work["steps"][0]["confirmed"] = False
        cases = [(changed_equation, work), (unconfirmed_problem, work),
                 (problem, unconfirmed_work)]
        for changed_problem, changed_work in cases:
            with self.subTest(problem=changed_problem, work=changed_work):
                for consume in [guided.error_evidence, guided.context, guided.student_feedback]:
                    with self.assertRaises(ValueError):
                        consume(changed_problem, changed_work, result)
        self.assertEqual(verify(changed_equation, work)["first_error_step_id"], "s1")
        self.assertEqual(guided.route(verify(problem, unconfirmed_work))["reason"],
                         "needs_input_confirmation")

    def test_result_without_matching_binding_or_version_is_rejected(self):
        problem, work, result = pair("x=2", ["x=2"])
        for field, value in [("input_fingerprint", None), ("input_fingerprint", "stale"),
                             ("verifier_version", "old-verifier"), ("problem_revision", "old")]:
            with self.subTest(field=field, value=value):
                changed = dict(result)
                if value is None:
                    del changed[field]
                else:
                    changed[field] = value
                with self.assertRaises(ValueError):
                    guided.student_feedback(problem, work, changed)

    def test_context_allowlist_omits_answers_and_review_metadata(self):
        problem, work, result = pair("(4/3)*x-3=x+6", ["(4/3)*x-3=x+6", "4*x-3=3*x+18"])
        result["reference_answer"] = "DO_NOT_SEND"
        result["expected"] = {"judgement": "incorrect"}
        full = guided.context(problem, work, result, "full")
        compact = guided.context(problem, work, result, "compact")
        for key in ["judgement", "first_error_step_id", "first_observed_error_step_id",
                    "earliest_unresolved_step_id", "completion", "action"]:
            self.assertEqual(full[key], compact[key])
        self.assertEqual(full["error_evidence"]["target_side"], compact["target_side"])
        for context in [full, compact]:
            self.assertNotIn("DO_NOT_SEND", repr(context))
            self.assertNotIn("reference_answer", context)
            self.assertNotIn("input_fingerprint", context)
        with self.assertRaises(ValueError):
            guided.context(problem, work, result, "invalid")

    def test_fixed_feedback_has_no_answer_and_does_not_mutate_inputs(self):
        problem, work, result = pair("(4/3)*x-3=x+6", ["(4/3)*x-3=x+6", "4*x-3=3*x+18"])
        saved = copy.deepcopy((problem, work, result))
        feedback = guided.student_feedback(problem, work, result)
        guided.context(problem, work, result)
        guided.route(result)
        self.assertEqual((problem, work, result), saved)
        self.assertNotIn("27", feedback["hint"])
        self.assertEqual(feedback["hint"], guided.SIDE_HINTS["equation"])
        for hint in list(policy.TEMPLATES.values()) + list(guided.SIDE_HINTS.values()):
            self.assertFalse(any(character.isdigit() for character in hint))
            self.assertNotIn("=", hint)


if __name__ == "__main__":
    unittest.main()
