"""교육 행동·도움 이력·검증 경계를 작은 가짜 모델로 확인한다."""

from copy import deepcopy
from io import BytesIO
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.tutor.session_model import OllamaTutorClient, OpenAICompatibleTutorClient, TutorModelError, _student_message, create_tutor_client
from app.tutor.session_service import initial_state, run_turn


PROBLEM = {"id": 1, "input_version_number": 1, "subject": "mathematics",
           "source_text": "다음 방정식을 풀어라.", "target_text": "2(x+2)=3(x-1)"}


class Model:
    def __init__(self, message="괄호 밖의 수가 안의 각 항에 어떻게 곱해지는지 확인해 볼까요?"):
        self.message, self.calls, self.context = message, 0, None

    def generate(self, problem, context, action):
        self.calls += 1
        self.context = context
        return {"message": self.message, "model": "test-only", "usage": {"input_tokens": 12, "output_tokens": 9}}


def request(intent="submit", *lines):
    return {"intent": intent, "student_text": "", "steps": [
        {"id": f"s{i}", "text": line, "state": "active"} for i, line in enumerate(lines, 1)
    ]}


class TutorPolicyTests(unittest.TestCase):
    def test_first_hint_has_no_student_attempt_and_keeps_input_unchanged(self):
        state, model = initial_state(), Model()
        before = deepcopy((PROBLEM, state))
        result = run_turn(PROBLEM, state, [], request("hint"), model_client=model)
        self.assertEqual(result["response"]["action"], "first_hint")
        self.assertEqual(result["state"]["attempts"], 0)
        self.assertTrue(result["state"]["assisted"])
        self.assertEqual(result["state"]["hint_count"], 1)
        self.assertEqual((PROBLEM, state), before)
        self.assertNotIn("verified_solution", model.context)

    def test_verified_error_and_corrected_work_preserve_assistance(self):
        wrong = run_turn(PROBLEM, initial_state(), [], request("submit", "2x+2=3x-3"), model_client=Model())
        self.assertEqual(wrong["response"]["verification"]["judgement"], "incorrect")
        self.assertEqual(wrong["response"]["evidence"]["target_side"], "lhs")
        corrected = run_turn(PROBLEM, wrong["state"], [], request("submit", "2x+4=3x-3", "x=7"), model_client=Model())
        self.assertEqual(corrected["response"]["action"], "complete")
        self.assertEqual(corrected["state"]["attempts"], 2)
        self.assertEqual(corrected["state"]["learning_status"], "solved_with_help")
        self.assertFalse(corrected["state"]["independent_verified"])
        self.assertTrue(corrected["state"]["needs_independent_check"])

    def test_last_right_answer_does_not_remove_active_wrong_step(self):
        result = run_turn(PROBLEM, initial_state(), [], request("submit", "2x+2=3x-3", "x=7"), model_client=Model())
        self.assertEqual(result["response"]["verification"]["judgement"], "incorrect")
        self.assertFalse(result["state"]["solved"])

    def test_constant_removal_hint_uses_verified_direction_without_a_model(self):
        cases = (
            ("(x-6)+(x-6)+(x-6)=18", ("(x-6)+(x-6)+(x-6)=18", "3x-18=18", "3x=38", "x=12"),
             "s3", "양변에 각각 18만큼 더해야", "오른쪽은 18 + 18"),
            ("14=2x+4", ("16=2x",), "s1", "양변에서 각각 4만큼 빼야", "왼쪽은 14 - 4"),
        )
        for equation, lines, error_step, direction, calculation in cases:
            with self.subTest(equation=equation):
                problem = dict(PROBLEM, target_text=equation)
                model = Model("3x - 18 = 18에서 18을 양변에서 빼면 어떻게 되나요?")
                wrong = run_turn(problem, initial_state(), [], request("submit", *lines), model_client=model)
                self.assertEqual(wrong["response"]["verification"]["first_error_step_id"], error_step)
                self.assertIn(direction, wrong["response"]["message"])
                self.assertNotIn(calculation, wrong["response"]["message"])
                extra = run_turn(problem, wrong["state"], [], request("more_help"), model_client=model)
                self.assertIn(calculation, extra["response"]["message"])
                self.assertEqual(extra["state"]["hint_count"], 2)
                self.assertTrue(extra["state"]["assisted"])
                self.assertFalse(extra["state"]["answer_revealed"])
                self.assertEqual(model.calls, 0)

    def test_fraction_first_hint_uses_the_sign_of_the_visible_constant(self):
        for equation, operation in (
            ("(3/2)*x-4=5", "양변에 각각 4만큼 더해야"),
            ("5=(3/2)*x+4", "양변에서 각각 4만큼 빼야"),
        ):
            with self.subTest(equation=equation):
                model = Model("양변에서 4를 빼세요.")
                result = run_turn(dict(PROBLEM, target_text=equation), {}, [], request("hint"), model_client=model)
                self.assertEqual(result["response"]["action"], "first_hint")
                self.assertEqual(result["response"]["source"], "verified_first_hint")
                self.assertIn(operation, result["response"]["message"])
                self.assertEqual(result["state"]["hint_count"], 1)
                self.assertFalse(result["state"]["answer_revealed"])
                self.assertEqual(model.calls, 0)

    def test_distribution_error_gets_verified_hint_and_additional_help(self):
        cases = (
            (PROBLEM["target_text"], "2x+2=3x-3", "왼쪽", "(2) × (2)"),
            ("2(x+2)=-3(x-1)", "2x+4=-3x-3", "오른쪽", "(-3) × (-1)"),
        )
        for equation, line, side, operation in cases:
            with self.subTest(equation=equation):
                problem, model = dict(PROBLEM, target_text=equation), Model("전개하면 2x+2가 맞아요.")
                wrong = run_turn(problem, {}, [], request("submit", line), model_client=model)
                extra = run_turn(problem, wrong["state"], [], request("more_help"), model_client=model)
                self.assertEqual(wrong["response"]["verification"]["first_error_step_id"], "s1")
                self.assertEqual(wrong["response"]["source"], "verified_error_hint")
                self.assertIn(side, wrong["response"]["message"])
                self.assertNotIn(operation, wrong["response"]["message"])
                self.assertIn(operation, extra["response"]["message"])
                self.assertEqual(extra["state"]["hint_count"], 2)
                self.assertFalse(extra["state"]["answer_revealed"])
                self.assertEqual(model.calls, 0)

    def test_verified_division_hint_quotes_the_wrong_student_line_and_keeps_the_next_step(self):
        model = Model("x=9가 정답입니다.")
        result = run_turn(dict(PROBLEM, target_text="3x=12"), {}, [], request("submit", "x=9"), model_client=model)
        self.assertEqual(result["response"]["source"], "verified_error_hint")
        self.assertEqual(result["response"]["verification"]["judgement"], "incorrect")
        self.assertIn("적은 식 ‘x=9’", result["response"]["message"])
        self.assertIn("나눌 수는 3", result["response"]["message"])
        self.assertNotIn("x=4", result["response"]["message"])
        self.assertFalse(result["state"]["answer_revealed"])
        self.assertEqual(model.calls, 0)

    def test_correct_prefix_gets_next_step_without_error_claim(self):
        result = run_turn(PROBLEM, initial_state(), [], request("submit", "2x+4=3x-3"), model_client=Model())
        self.assertEqual(result["response"]["action"], "next_step")
        self.assertEqual(result["response"]["verification"]["judgement"], "correct")
        self.assertFalse(result["state"]["solved"])

    def test_corrected_prefix_does_not_repeat_historical_error(self):
        wrong = run_turn(PROBLEM, initial_state(), [], request("submit", "2x+2=3x-3"), model_client=Model())
        events = [{"student_text": "", "response": wrong["response"]}]
        model = Model("지금은 맞지만 좌변 2x+2라고 쓴 부분은 틀렸습니다. 다시 계산해 주세요.")
        corrected = run_turn(PROBLEM, wrong["state"], events, request("submit", "2x+4=3x-3"), model_client=model)
        self.assertEqual(corrected["response"]["action"], "next_step")
        self.assertEqual(corrected["response"]["verification"]["judgement"], "correct")
        self.assertNotIn("틀렸", corrected["response"]["message"])
        self.assertNotIn("2x+2", corrected["response"]["message"])
        self.assertEqual(corrected["response"]["output_guard"], "current_verified_work_preserved")
        self.assertEqual(corrected["response"]["usage"]["output_tokens"], 9)
        self.assertEqual(model.context["current_work_status"], "verified_correct_incomplete")
        self.assertNotIn("message", model.context["recent_dialogue"][0])

    def test_identical_hint_after_valid_progress_is_replaced_by_a_verified_next_step(self):
        first = run_turn(PROBLEM, initial_state(), [], request("hint"), model_client=Model())
        repeated = Model(first["response"]["message"].replace(" ", "  "))
        result = run_turn(PROBLEM, first["state"], [{"response": first["response"]}],
                          request("submit", "2x+4=3x-3"), model_client=repeated)
        self.assertEqual(result["response"]["output_guard"], "repeated_hint_replaced")
        self.assertIn("4=x-3", result["response"]["message"])
        self.assertNotIn("x=7", result["response"]["message"])
        self.assertEqual(result["response"]["verification"]["judgement"], "correct")

    def test_more_help_shows_one_verified_step_without_calling_model_or_final_answer(self):
        prefix = run_turn(PROBLEM, initial_state(), [], request("submit", "2x+4=3x-3"), model_client=Model())
        model = Model()
        result = run_turn(PROBLEM, prefix["state"], [], request("more_help"), model_client=model)
        self.assertEqual(result["response"]["source"], "verified_scaffold")
        self.assertIn("4=x-3", result["response"]["message"])
        self.assertNotIn("x=7", result["response"]["message"])
        self.assertEqual(model.calls, 0)
        self.assertTrue(result["state"]["assisted"])
        self.assertEqual(result["state"]["hint_count"], prefix["state"]["hint_count"] + 1)
        self.assertEqual(result["state"]["help_level"], 2)
        self.assertEqual(result["state"]["attempts"], prefix["state"]["attempts"])

    def test_new_freeform_submission_does_not_reverify_old_completed_steps(self):
        previous = run_turn(PROBLEM, initial_state(), [], request("submit", "x=7"), model_client=Model())
        body = request("submit")
        body["student_text"] = "모르겠어요."
        result = run_turn(PROBLEM, previous["state"], [], body, model_client=Model())
        self.assertEqual(result["response"]["action"], "guide")
        self.assertFalse(result["state"]["solved"])
        self.assertEqual(result["state"]["last_steps"], [])
        self.assertEqual(result["state"]["attempts"], 1)

    def test_uncertain_line_requests_confirmation_without_model_call(self):
        model, body = Model(), request("submit", "x?7")
        body["steps"][0]["state"] = "uncertain"
        result = run_turn(PROBLEM, initial_state(), [], body, model_client=model)
        self.assertEqual(result["response"]["action"], "ask_confirmation")
        self.assertEqual(result["response"]["verification"]["judgement"], "undetermined")
        self.assertEqual(model.calls, 0)

    def test_unsupported_problem_still_allows_guidance_without_tool_proof(self):
        problem = dict(PROBLEM, target_text="(4x^2+5)+(2x-6)을 간단히 하세요.")
        model = Model()
        result = run_turn(problem, initial_state(), [], request("hint"), model_client=model)
        self.assertEqual(result["response"]["action"], "first_hint")
        self.assertFalse(result["response"]["tool_supported"])
        self.assertEqual(result["response"]["verification"]["judgement"], "undetermined")
        self.assertNotIn("invalid_syntax", json.dumps(model.context))

    def test_expression_marks_first_error_checks_completion_and_requests_clarification(self):
        problem = dict(PROBLEM, target_text="(4x+5)+(2x-6)을 간단히 하세요.")
        model = Model()
        wrong = run_turn(problem, {}, [], request("submit", "6x+1", "6x-1"), model_client=model)
        result = wrong["response"]["verification"]
        self.assertEqual(result["first_error_step_id"], "s1")
        self.assertEqual(result["judgement"], "incorrect")
        self.assertIn("상수항", wrong["response"]["message"])
        self.assertFalse(wrong["state"]["solved"])

        valid = run_turn(problem, wrong["state"], [], request("submit", "4x+5+2x-6"), model_client=model)
        self.assertEqual(valid["response"]["verification"]["completion"], "incomplete")
        solved = run_turn(problem, valid["state"], [], request("submit", "4x+5+2x-6=6x-1"), model_client=model)
        self.assertTrue(solved["state"]["solved"])
        self.assertEqual(solved["response"]["target_analysis"]["status"], "linear_expression")

        uncertain = run_turn(problem, {}, [], request("submit", "6x+?"), model_client=model)
        self.assertEqual(uncertain["response"]["action"], "ask_confirmation")
        self.assertEqual(uncertain["response"]["verification"]["judgement"], "undetermined")
        self.assertFalse(uncertain["state"]["solved"])
        self.assertEqual(model.calls, 0)

    def test_numbered_subquestions_request_selection_without_calling_a_model(self):
        targets = ("다음 방정식을 풀어라.\n(1) 4/3x-3=x+6\n(2) 1/4x-1/2=-2/5x+2\n(3) 2/3x-1/6=3/2x",
                   "\n".join(("다음 방정식을 풀어라.", r"(1) $\frac{4}{3}x-3=x+6$",
                              r"(2) $\frac{1}{4}x-\frac{1}{2}=-\frac{2}{5}x+2$",
                              r"(3) $\frac{2}{3}x-\frac{1}{6}=\frac{3}{2}x$")))
        for target in targets:
            problem = dict(PROBLEM, source_text=target, target_text=target)
            original = deepcopy(problem)
            for intent in ("hint", "submit", "answer"):
                with self.subTest(target=target, intent=intent):
                    model = Model("저는 먼저 1번을 선택하고 양변에 3을 곱해볼게요.")
                    result = run_turn(problem, initial_state(), [], request(intent, "4x-9=3x+18", "x=27",
                        "5x-10=-8x+40", "x=-50/13", "x=-1/5"), model_client=model)
                    self.assertEqual(result["response"]["action"], "select_target")
                    self.assertEqual(result["response"]["reason"], "multiple_subquestions")
                    self.assertEqual(len(result["response"]["target_analysis"]["choices"]), 3)
                    self.assertEqual(model.calls, 0)
                    self.assertEqual(result["state"]["attempts"], 0)
                    self.assertEqual(result["state"]["hint_count"], 0)
                    self.assertFalse(result["state"]["answer_revealed"])
            self.assertEqual(problem, original)

    def test_selected_fraction_equations_complete_without_repeating_the_first_hint(self):
        cases = ((r"\frac{4}{3}x-3=x+6", ("4x-9=3x+18", "x=27")),
                 (r"\frac{2}{3}x-\frac{1}{6}=\frac{3}{2}x", ("4x-1=9x", "x=-1/5")))
        for target, lines in cases:
            with self.subTest(target=target):
                problem, model = dict(PROBLEM, target_text=target), Model("저는 1번을 선택하고 양변에 3을 곱해볼게요.")
                original = deepcopy(problem)
                result = run_turn(problem, initial_state(), [], request("submit", *lines), model_client=model)
                self.assertTrue(result["response"]["tool_supported"])
                self.assertEqual(result["response"]["action"], "complete")
                self.assertEqual(model.calls, 0)
                self.assertEqual(problem, original)

    def test_selected_second_fraction_equation_reports_the_actual_sign_error(self):
        problem = dict(PROBLEM, target_text=r"\frac{1}{4}x-\frac{1}{2}=-\frac{2}{5}x+2", attempt_mode="independent")
        model = Model()
        result = run_turn(problem, initial_state(), [], request("submit", "5x-10=-8x+40", "x=-50/13"), model_client=model)
        self.assertTrue(result["response"]["tool_supported"])
        self.assertEqual(result["response"]["verification"]["judgement"], "incorrect")
        self.assertEqual(result["response"]["verification"]["first_error_step_id"], "s2")
        self.assertEqual(model.calls, 0)

    def test_confirmed_unsupported_work_is_recorded_without_using_a_model(self):
        model = Model()
        result = run_turn(PROBLEM, initial_state(), [], request("submit", "양변에서 같은 수를 뺐어요."), model_client=model)
        self.assertEqual(result["response"]["action"], "undetermined")
        self.assertEqual(result["response"]["verification"]["reason_code"], "unsupported_observation")
        self.assertFalse(result["response"]["tool_supported"])
        self.assertEqual(model.calls, 0)

    def test_variable_denominator_explains_pending_verdict_for_plain_and_ocr_targets(self):
        ocr = "\n".join((r"방정식 \(\frac{1}{2-\frac{1}{x}}=\frac{2}{3}\)를 풀면?",
                         r"① \(x=1\)", r"② \(x=2\)"))
        for target in ("1/(2-1/x)=2/3", ocr):
            for answer in ("x=2", "x=-2"):
                with self.subTest(target=target, answer=answer):
                    problem = dict(PROBLEM, source_text=ocr, target_text=target)
                    model = Model()
                    work = request("submit", "x/(2x-1)=2/3", "3x=4x-2", answer)
                    result = run_turn(problem, initial_state(), [], work, model_client=model)
                    response, state = result["response"], result["state"]
                    self.assertEqual(response["action"], "undetermined")
                    self.assertEqual(response["verification"]["judgement"], "undetermined")
                    self.assertEqual(response["verification"]["reason_code"], "variable_denominator")
                    self.assertIn("분모에 문자가 있어", response["message"])
                    self.assertEqual(model.calls, 0)
                    self.assertEqual(state["last_steps"], work["steps"])
                    self.assertEqual(state["attempts"], 1)
                    self.assertEqual(state["hint_count"], 0)
                    self.assertFalse(state["solved"])
                    help_result = run_turn(problem, state, [], request("more_help"), model_client=model)
                    self.assertEqual(help_result["response"]["action"], "scaffold")
                    self.assertEqual(model.calls, 1)

    def test_direct_answer_request_records_exposure_without_solving_for_student(self):
        model = Model("양변을 정리하면 x=7이에요.")
        events = [{"student_text": "모르겠어요", "response": {"message": "어떻게 계산하면 될까요?"}}]
        result = run_turn(PROBLEM, initial_state(), events, request("answer"), model_client=model)
        self.assertEqual(result["response"]["action"], "answer")
        self.assertEqual(model.context["verified_solution"], {"variable": "x", "value": "7"})
        self.assertTrue(result["state"]["answer_revealed"])
        self.assertTrue(result["state"]["assisted"])
        self.assertFalse(result["state"]["solved"])
        self.assertFalse(result["state"]["independent_verified"])
        self.assertEqual(result["state"]["attempts"], 0)
        self.assertEqual(model.context["recent_dialogue"], [])
        self.assertEqual(model.context["required_output"], "final_answer_first_then_worked_explanation_and_check")

    def test_answer_leak_in_hint_is_replaced_and_recorded(self):
        result = run_turn(PROBLEM, initial_state(), [], request("hint"), model_client=Model("정답은 x=7입니다."))
        self.assertNotIn("x=7", result["response"]["message"])
        self.assertEqual(result["response"]["source"], "guarded_model")
        self.assertFalse(result["state"]["answer_revealed"])

    def test_latex_reversed_and_prose_answers_are_blocked_without_exposure(self):
        for text in (r"답은 \(x=\frac{7}{1}\)입니다.", "7=x입니다.", "해는 7이에요.", "x의 값은 (7/1)이에요.", "정답=7", "x는 7이에요."):
            with self.subTest(text=text):
                result = run_turn(PROBLEM, initial_state(), [], request("hint"), model_client=Model(text))
                self.assertEqual(result["response"]["output_guard"], "terminal_answer_removed")
                self.assertTrue(result["response"]["answer_exposure_blocked"])
                self.assertFalse(result["state"]["answer_revealed"])

    def test_wrong_generated_answer_or_work_is_replaced_by_calculated_answer(self):
        for text in ("정답은 x=8이에요.", "정답은 7이에요. 전개하면 2x+2=3x-3이에요.", "정답은 8이에요.", "정답은 7이에요. 2+2=5예요.", "y는 7이에요."):
            with self.subTest(text=text):
                result = run_turn(PROBLEM, initial_state(), [], request("answer"), model_client=Model(text))
                self.assertEqual(result["response"]["source"], "verified_answer")
                self.assertIn("x=7", result["response"]["message"])
                self.assertFalse(result["response"]["output_verification"]["prose_verified"])
                self.assertTrue(result["state"]["answer_revealed"])
                self.assertFalse(result["state"]["solved"])

    def test_observed_false_expansion_in_natural_language_is_not_published(self):
        message = "2(x+2)를 전개하면 2x+2가 되어야 해요. 왼쪽을 다시 확인해 볼까요?"
        result = run_turn(PROBLEM, initial_state(), [], request("submit", "x+4=3x-3"), model_client=Model(message))
        self.assertEqual(result["response"]["output_guard"], "generated_math_not_verified")
        self.assertNotIn("2x+2", result["response"]["message"])
        self.assertIn("왼쪽", result["response"]["message"])
        self.assertEqual(result["response"]["verification"]["judgement"], "incorrect")

    def test_correct_expansion_relation_is_retained_without_claiming_prose_verification(self):
        message = "2(x+2)를 전개하면 2x+4가 돼요. 왼쪽의 상수항을 비교해 볼까요?"
        result = run_turn(PROBLEM, initial_state(), [], request("submit", "x+4=3x-3"), model_client=Model(message))
        self.assertEqual(result["response"]["message"], message)
        self.assertTrue(result["response"]["output_verification"]["expansions_checked"])
        self.assertFalse(result["response"]["output_verification"]["prose_verified"])

    def test_correct_generated_equations_are_checked_without_verifying_all_prose(self):
        text = r"정답은 \(x=\frac{7}{1}\)이에요. 2x+4=3x-3에서 -x=-7을 얻어요."
        result = run_turn(PROBLEM, initial_state(), [], request("answer"), model_client=Model(text))
        self.assertEqual(result["response"]["message"], text)
        self.assertEqual(result["response"]["output_verification"]["status"], "checked")
        self.assertFalse(result["response"]["output_verification"]["prose_verified"])

    def test_correct_equations_in_a_list_keep_the_full_answer(self):
        message = "x = 7\n\n1. 양변을 전개해요.\n   - 2x + 4 = 3x - 3\n   - -x = -7\n- 7 = x"
        result = run_turn(PROBLEM, {}, [], request("answer"), model_client=Model(message))
        self.assertEqual(result["response"]["message"], message)
        self.assertEqual(result["response"]["source"], "model")
        self.assertEqual(result["response"]["output_verification"]["status"], "checked")

    def test_negative_equations_keep_their_sign(self):
        for equation in ("-x = -7", "− x = -7", "$$\n- x = -7\n$$",
                         "\\[\n- x = -7\n\\]", "```\n- x = -7\n```"):
            with self.subTest(equation=equation):
                message = f"x = 7\n\n{equation}"
                result = run_turn(PROBLEM, {}, [], request("answer"), model_client=Model(message))
                self.assertEqual(result["response"]["message"], message)
                self.assertEqual(result["response"]["output_verification"]["status"], "checked")

    def test_list_format_does_not_bypass_math_or_answer_exposure_checks(self):
        for equation in ("- 2x + 2 = 3x - 3", "- -x = 7", "$$\n- x = 7\n$$"):
            with self.subTest(equation=equation):
                model = Model(f"x = 7\n\n{equation}")
                result = run_turn(PROBLEM, {}, [], request("answer"), model_client=model)
                self.assertEqual(result["response"]["output_guard"], "generated_answer_replaced")

        result = run_turn(PROBLEM, {}, [], request("hint"), model_client=Model("- x = 7"))
        self.assertEqual(result["response"]["output_guard"], "terminal_answer_removed")
        self.assertTrue(result["response"]["answer_exposure_blocked"])
        self.assertFalse(result["state"]["answer_revealed"])

    def test_independent_and_review_submissions_do_not_receive_automatic_help(self):
        for mode in ("independent", "review"):
            for lines, expected in ((["2x+2=3x-3"], "retry"), (["2x+4=3x-3"], "continue"), (["x=7"], "complete")):
                with self.subTest(mode=mode, lines=lines):
                    model = Model()
                    result = run_turn(dict(PROBLEM, attempt_mode=mode), initial_state(), [], request("submit", *lines), model_client=model)
                    self.assertEqual(model.calls, 0)
                    self.assertEqual(result["response"]["action"], expected)
                    self.assertFalse(result["state"]["assisted"])
                    self.assertEqual(result["state"]["independent_verified"], expected == "complete")
                    if expected == "complete":
                        self.assertEqual(result["state"]["learning_status"], mode + "_verified")
                        self.assertFalse(result["state"]["needs_independent_check"])

    def test_explicit_help_prevents_later_independent_success(self):
        problem = dict(PROBLEM, attempt_mode="independent")
        for intent in ("hint", "more_help", "answer"):
            with self.subTest(intent=intent):
                helped = run_turn(problem, initial_state(), [], request(intent), model_client=Model("정답은 x=7이에요."))
                result = run_turn(problem, helped["state"], [], request("submit", "x=7"), model_client=Model())
                self.assertTrue(result["state"]["assisted"])
                self.assertFalse(result["state"]["independent_verified"])
                self.assertTrue(result["state"]["needs_independent_check"])

    def test_unsupported_independent_work_stays_undetermined_without_a_model(self):
        model = Model()
        result = run_turn(dict(PROBLEM, attempt_mode="independent"), initial_state(), [], request("submit", "x^2=49"), model_client=model)
        self.assertEqual(result["response"]["action"], "undetermined")
        self.assertEqual(result["response"]["verification"]["judgement"], "undetermined")
        self.assertFalse(result["state"]["independent_verified"])
        self.assertEqual(model.calls, 0)

    def test_repeat_request_snapshot_does_not_add_student_attempt(self):
        body = request("submit", "2x+2=3x-3")
        first = run_turn(PROBLEM, initial_state(), [], body, model_client=Model())
        repeated = run_turn(PROBLEM, first["state"], [], body, model_client=Model())
        self.assertEqual(repeated["state"]["attempts"], 1)
        extra = run_turn(PROBLEM, repeated["state"], [], request("more_help"), model_client=Model())
        self.assertEqual(extra["response"]["action"], "scaffold")
        self.assertEqual(extra["state"]["attempts"], 1)
        self.assertEqual(extra["state"]["help_level"], 2)

    def test_completed_without_recorded_help_is_not_global_mastery(self):
        model = Model()
        result = run_turn(PROBLEM, initial_state(), [], request("submit", "x=7"), model_client=model)
        self.assertEqual(result["state"]["learning_status"], "solved_without_recorded_help")
        self.assertFalse(result["state"]["independent_verified"])
        self.assertEqual(model.calls, 0)

    def test_model_failure_does_not_modify_given_state(self):
        class Broken:
            def generate(self, *args):
                raise TutorModelError("테스트 실패")
        state = initial_state()
        with self.assertRaises(TutorModelError):
            run_turn(PROBLEM, state, [], request("hint"), model_client=Broken())
        self.assertEqual(state, initial_state())

    def test_private_reasoning_is_not_student_message(self):
        self.assertEqual(_student_message("<think>private</think>힌트입니다."), "힌트입니다.")
        self.assertEqual(_student_message("<think>unfinished private"), "")

    def test_gateway_bounded_hint_and_incomplete_stream_is_not_published(self):
        for reason in ("stop", "length"):
            with self.subTest(reason=reason):
                events = [
                    {"message": {"content": "<think>private</think>왼쪽 괄호를 확인해 볼까요?"}},
                    {"done": True, "done_reason": reason, "model": "test-only", "prompt_eval_count": 20, "eval_count": 12},
                ]
                stream = BytesIO(b"".join(json.dumps(event).encode() + b"\n" for event in events))
                with patch("app.tutor.session_model.urlopen", return_value=stream) as call:
                    if reason == "length":
                        with self.assertRaises(TutorModelError):
                            OllamaTutorClient().generate(PROBLEM, {"error_evidence": {"target_side": "lhs"}}, "error_hint")
                    else:
                        result = OllamaTutorClient().generate(PROBLEM, {"error_evidence": {"target_side": "lhs"}}, "error_hint")
                        self.assertEqual(result["message"], "왼쪽 괄호를 확인해 볼까요?")
                        self.assertEqual(result["usage"]["output_tokens"], 12)
                        self.assertIsNone(result["usage"]["reasoning_tokens"])
                        self.assertEqual(result["usage"]["output_token_semantics"], "ollama_eval_count_reasoning_inclusion_unspecified")
                    payload = json.loads(call.call_args.args[0].data)
                    self.assertEqual(payload["options"]["num_predict"], 480)

    def test_gateway_answer_request_has_distinct_positive_output_instruction(self):
        for action in ("hint", "answer", "scaffold"):
            stream = BytesIO(json.dumps({"message": {"content": "답과 설명입니다."}, "done": True, "done_reason": "stop"}).encode() + b"\n")
            with patch("app.tutor.session_model.urlopen", return_value=stream) as call:
                result = OllamaTutorClient().generate(PROBLEM, {}, action)
            payload = json.loads(call.call_args.args[0].data)
            system = payload["messages"][0]["content"]
            self.assertEqual("이번 요청은 답·전체 해설 공개" in system, action == "answer")
            self.assertEqual("이번 요청은 추가 도움" in system, action == "scaffold")
            if action == "answer":
                self.assertIn("최종 답을 첫 문장에 반드시 제시", system)
                self.assertIn("이번 문제는 자동 검증 도구의 지원 범위 밖", system)
                self.assertIn("검산 단락을 억지로 붙이거나", system)
                self.assertEqual(payload["options"]["num_predict"], 900)
            self.assertEqual(result["usage"]["prompt_version"], "didgo-tutor-prompt-v6")

    def test_openai_compatible_stream_has_provider_usage_and_no_forced_thinking_option(self):
        events = [
            {"model": "local-korean-tutor", "choices": [{"delta": {"reasoning_content": "숨긴 추론"}}]},
            {"choices": [{"delta": {"content": "한국어 힌트예요."}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 31, "completion_tokens": 9,
                                      "completion_tokens_details": {"reasoning_tokens": 4}}},
        ]
        stream = BytesIO(b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in events) + b"data: [DONE]\n")
        settings = {"DIDGO_TUTOR_PROVIDER": "openai-compatible", "DIDGO_TUTOR_URL": "http://127.0.0.1:9000/v1", "DIDGO_TUTOR_MODEL": "local-korean-tutor", "DIDGO_TUTOR_PROFILE": "openai-usage"}
        with patch.dict(os.environ, settings), patch("app.tutor.session_model.urlopen", return_value=stream) as call:
            client = create_tutor_client()
            self.assertIsInstance(client, OpenAICompatibleTutorClient)
            result = client.generate(PROBLEM, {}, "hint")
        payload = json.loads(call.call_args.args[0].data)
        self.assertNotIn("think", payload)
        self.assertEqual(payload["stream_options"], {"include_usage": True})
        self.assertEqual(call.call_args.args[0].full_url, "http://127.0.0.1:9000/v1/chat/completions")
        self.assertEqual(result["message"], "한국어 힌트예요.")
        self.assertEqual(result["usage"]["provider"], "openai-compatible")
        self.assertEqual(result["usage"]["input_tokens"], 31)
        self.assertEqual(result["usage"]["reasoning_tokens"], 4)
        self.assertEqual(result["usage"]["output_tokens"], 9)
        self.assertEqual(result["usage"]["output_token_semantics"], "openai_completion_tokens_including_reasoning")
        self.assertIn("model_request_seconds", result["usage"])
        self.assertNotIn("request_seconds", result["usage"])

    def test_provider_profiles_do_not_send_unsupported_options_implicitly(self):
        event = {"message": {"content": "힌트예요."}, "done": True, "done_reason": "stop"}
        for profile in ("standard", "ollama-no-think"):
            with self.subTest(profile=profile), patch.dict(os.environ, {"DIDGO_TUTOR_PROFILE": profile}), patch("app.tutor.session_model.urlopen", return_value=BytesIO(json.dumps(event).encode() + b"\n")) as call:
                OllamaTutorClient().generate(PROBLEM, {}, "hint")
                self.assertEqual("think" in json.loads(call.call_args.args[0].data), profile == "ollama-no-think")

    def test_openai_truncated_output_is_not_a_success_and_reasoning_profile_is_explicit(self):
        settings = {"DIDGO_TUTOR_PROVIDER": "openai-compatible", "DIDGO_TUTOR_URL": "http://127.0.0.1:9000", "DIDGO_TUTOR_MODEL": "another-tutor", "DIDGO_TUTOR_PROFILE": "openai-reasoning"}
        event = {"choices": [{"delta": {"content": "끝나지 않은 설명"}, "finish_reason": "length"}]}
        stream = BytesIO(b"data: " + json.dumps(event).encode() + b"\n\ndata: [DONE]\n")
        with patch.dict(os.environ, settings), patch("app.tutor.session_model.urlopen", return_value=stream) as call:
            with self.assertRaises(TutorModelError):
                create_tutor_client().generate(PROBLEM, {}, "answer")
        payload = json.loads(call.call_args.args[0].data)
        self.assertIn("max_completion_tokens", payload)
        self.assertNotIn("temperature", payload)
        self.assertNotIn("think", payload)

    def test_guide_prompt_uses_current_work_and_keeps_the_tutor_role(self):
        stream = BytesIO(json.dumps({"message": {"content": "현재 풀이에서 어떤 부분을 확인하고 싶나요?"},
                                    "done": True, "done_reason": "stop"}).encode() + b"\n")
        context = {"steps": [{"id": "s1", "text": "이미 적은 설명", "state": "active"}]}
        with patch("app.tutor.session_model.urlopen", return_value=stream) as call:
            OllamaTutorClient().generate(PROBLEM, context, "guide")
        payload = json.loads(call.call_args.args[0].data)
        self.assertIn("현재 steps와 질문을 먼저 읽으세요", payload["messages"][0]["content"])
        self.assertIn("학생 역할로 말하지 말고", payload["messages"][0]["content"])
        self.assertEqual(json.loads(payload["messages"][1]["content"])["context"]["steps"], context["steps"])


if __name__ == "__main__":
    unittest.main()
