"""교육 행동·도움 이력·검증 경계를 작은 가짜 모델로 확인한다."""

from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.tutor.session_model import OllamaTutorClient, TutorModelError, _student_message
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
        problem = dict(PROBLEM, target_text="(4x+5)+(2x-6)을 간단히 하세요.")
        result = run_turn(problem, initial_state(), [], request("hint"), model_client=Model())
        self.assertEqual(result["response"]["action"], "first_hint")
        self.assertFalse(result["response"]["tool_supported"])
        self.assertEqual(result["response"]["verification"]["judgement"], "undetermined")

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
            self.assertEqual(result["usage"]["prompt_version"], "didgo-tutor-prompt-v4")


if __name__ == "__main__":
    unittest.main()
