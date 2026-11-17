# 일차방정식 검증기의 합성 회귀 사례
# 실행: python3 -B -m unittest discover -s backend/tests -p 'test_step_verifier.py' -v
# 정상·오류·미확인·삭제·지원 범위와 완료 상태를 검사함


from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.tutor.step_verifier import verify


def problem(equation="2(x+3)=10", **changes):
    return {"schema_version": "verification-problem-v1", "id": "p1", "revision": "v1",
            "equation": equation, "variable": "x", "domain": "real", "confirmed": True,
            "profile": "linear-equation-v1", **changes}


def work(*texts):
    return {"schema_version": "verification-work-v1", "problem_id": "p1", "problem_revision": "v1",
            "steps": [{"id": f"s{i}", "text": text, "state": "active", "confirmed": True}
                      for i, text in enumerate(texts, 1)]}


class StepVerifierTests(unittest.TestCase):
    def test_standard_path(self):
        result = verify(problem(), work("2(x+3)=10", "2x+6=10", "2x=4", "x=2"))
        self.assertEqual((result["judgement"], result["completion"], result["valid_prefix_until_step_id"]),
                         ("correct", "complete", "s4"))
        self.assertIsNone(result["first_error_step_id"])

    def test_alternative_path_and_skipped_operations(self):
        for path in [("x+3=5", "x=2"), ("x=2",), ("2=x",), ("(x)=4/2",)]:
            with self.subTest(path=path):
                result = verify(problem(), work(*path))
                self.assertEqual(result["judgement"], "correct")
                self.assertEqual(result["completion"], "complete")

    def test_correct_prefix_incomplete(self):
        result = verify(problem(), work("x+3=5"))
        self.assertEqual((result["judgement"], result["completion"]), ("correct", "incomplete"))

    def test_right_answer_does_not_erase_wrong_work(self):
        result = verify(problem(), work("2(x+3)=10", "2x+3=10", "x=2"))
        self.assertEqual(result["judgement"], "incorrect")
        self.assertEqual(result["first_error_step_id"], "s2")
        self.assertEqual(result["first_observed_error_step_id"], "s2")
        self.assertEqual(result["completion"], "complete")
        self.assertEqual(result["valid_prefix_until_step_id"], "s1")

    def test_wrong_answer_is_incomplete(self):
        result = verify(problem(), work("x=3"))
        self.assertEqual((result["judgement"], result["completion"]), ("incorrect", "incomplete"))

    def test_unknown_before_error_cannot_establish_earliest_error(self):
        value = work("2x+6=10", "x+3=5", "x=3")
        value["steps"][1]["confirmed"] = False
        result = verify(problem(), value)
        self.assertEqual(result["judgement"], "incorrect")
        self.assertEqual(result["action"], "ask_confirmation")
        self.assertIsNone(result["first_error_step_id"])
        self.assertEqual(result["first_observed_error_step_id"], "s3")
        self.assertEqual(result["earliest_unresolved_step_id"], "s2")
        self.assertEqual(result["valid_prefix_until_step_id"], "s1")

    def test_unknown_after_error_preserves_confirmed_error(self):
        value = work("x=3", "x=2")
        value["steps"][1]["state"] = "uncertain"
        result = verify(problem(), value)
        self.assertEqual(result["first_error_step_id"], "s1")
        self.assertEqual(result["completion"], "unknown")

    def test_unknown_input_is_not_student_error(self):
        for text in (r"\frac{x}{2}=1", "x?2", "정답은 x=2", "x==2", "x=", "2 3x=6"):
            with self.subTest(text=text):
                result = verify(problem(), work(text))
                self.assertEqual(result["judgement"], "undetermined")
                self.assertEqual(result["action"], "ask_confirmation")
                self.assertIsNone(result["first_error_step_id"])

    def test_empty_unreadable_regions_keep_their_location(self):
        for text, state, confirmed in [("", "uncertain", False), (" ", "active", False),
                                       ("", "active", True)]:
            with self.subTest(text=text, state=state, confirmed=confirmed):
                value = work(text, "x=3")
                value["steps"][0].update(state=state, confirmed=confirmed)
                result = verify(problem(), value)
                self.assertEqual(result["action"], "ask_confirmation")
                self.assertEqual(result["earliest_unresolved_step_id"], "s1")
                self.assertIsNone(result["first_error_step_id"])
                self.assertEqual(result["first_observed_error_step_id"], "s2")

    def test_confirmed_deletion_excludes_wrong_line(self):
        value = work("x=3", "x=2")
        value["steps"][0]["state"] = "deleted"
        result = verify(problem(), value)
        self.assertEqual(result["judgement"], "correct")
        self.assertTrue(result["steps"][0]["skipped"])
        self.assertEqual(result["steps"][1]["previous_step_id"], None)

    def test_unconfirmed_deletion_is_not_silently_discarded(self):
        value = work("x=3", "x=2")
        value["steps"][0].update(state="deleted", confirmed=False)
        result = verify(problem(), value)
        self.assertEqual(result["judgement"], "undetermined")
        self.assertEqual(result["earliest_unresolved_step_id"], "s1")
        self.assertFalse(result["steps"][0]["skipped"])

    def test_no_active_work_routes_to_first_hint(self):
        for value in (work(), work("x=3")):
            if value["steps"]:
                value["steps"][0]["state"] = "deleted"
            result = verify(problem(), value)
            self.assertEqual((result["judgement"], result["action"], result["completion"]),
                             ("undetermined", "first_hint", "no_work"))

    def test_exact_fraction_decimal_and_minus(self):
        for equation, steps in [("x/2+1=2", ["x/2=1", "x=2"]),
                                ("0.1x=0.3", ["x=3"]),
                                ("−x=−2", ["x=2"]),
                                ("(1/2)x=1", ["x=2"]),
                                ("2（x+3）＝10", ["2×x=4", "x=2"]),
                                ("x+2(3)=8", ["x=2"])]:
            with self.subTest(equation=equation):
                result = verify(problem(equation), work(*steps))
                self.assertEqual((result["judgement"], result["completion"]), ("correct", "complete"))

    def test_ambiguous_division_and_identifier_are_held(self):
        for text in ("1/2x=1", "2/3(x+1)=2", "1/-2x=-1", "x1=2"):
            with self.subTest(text=text):
                result = verify(problem(), work(text))
                self.assertEqual(result["judgement"], "undetermined")
                self.assertEqual(result["action"], "ask_confirmation")

    def test_supported_explicit_fraction_product(self):
        result = verify(problem(), work("(1/2)*x=1", "x=2"))
        self.assertEqual(result["judgement"], "correct")

    def test_student_nonlinear_or_variable_denominator_is_held(self):
        for text in ("x*x=4", "x**2=4", "x^2=4", "1/x=1/2", "x/(x-x+1)=2"):
            with self.subTest(text=text):
                result = verify(problem(), work(text))
                self.assertEqual(result["judgement"], "undetermined")
                self.assertIsNone(result["first_error_step_id"])

    def test_identity_contradiction_and_zero_division_are_not_equivalent(self):
        for text in ("0=0", "0=1", "x/0=2"):
            with self.subTest(text=text):
                result = verify(problem(), work(text))
                self.assertEqual(result["judgement"], "incorrect")
                self.assertEqual(result["first_error_step_id"], "s1")

    def test_unsupported_problem_never_judges_student(self):
        for original in [problem("x*x=4"), problem("1/x=1"), problem("0x=1"), problem("x=x"),
                         problem(domain="complex"), problem(profile="geometry-v1")]:
            with self.subTest(original=original):
                result = verify(original, work("x=3"))
                self.assertEqual((result["judgement"], result["action"]), ("undetermined", "unsupported"))
                self.assertIsNone(result["first_error_step_id"])

    def test_unconfirmed_problem_and_stale_revision(self):
        p, w = problem(), work("x=3")
        for change, expected_code in [("revision", "problem_revision_mismatch"),
                                      ("problem_id", "problem_revision_mismatch"),
                                      ("confirmed", "problem_unconfirmed")]:
            with self.subTest(change=change):
                a, b = deepcopy(p), deepcopy(w)
                if change == "confirmed":
                    a[change] = False
                else:
                    b["problem_revision" if change == "revision" else change] = "other"
                result = verify(a, b)
                self.assertEqual((result["judgement"], result["action"]), ("undetermined", "ask_confirmation"))
                self.assertEqual(result["reason_code"], expected_code)

    def test_contract_errors_are_explicit(self):
        values = []
        a, b = problem(), work("x=2"); a["extra"] = True; values.append((a, b))
        a, b = problem(), work("x=2"); del a["domain"]; values.append((a, b))
        a, b = problem(), work("x=2"); a["confirmed"] = 1; values.append((a, b))
        a, b = problem(), work("x=2"); b["steps"][0]["confirmed"] = 1; values.append((a, b))
        a, b = problem(), work("x=2", "x=2"); b["steps"][1]["id"] = "s1"; values.append((a, b))
        a, b = problem(), work("x=2"); b["steps"][0]["state"] = "approved"; values.append((a, b))
        a, b = problem(), work("x=2"); b["steps"][0]["text"] = 2; values.append((a, b))
        a, b = problem(), work("x=2"); a["variable"] = "xy"; values.append((a, b))
        a, b = problem(), work("x=2"); b["steps"] *= 101; values.append((a, b))
        for a, b in values:
            with self.subTest(problem=a, work=b):
                with self.assertRaises(ValueError): verify(a, b)

    def test_input_limits_and_code_are_not_executed(self):
        for text in ("(" * 40 + "x" + ")" * 40 + "=2", "-" * 40 + "x=2",
                     "9" * 40 + "x=2", "x+" * 200 + "x=2", "x=" + "1" * 600,
                     "__import__('os').system('echo unsafe')=2"):
            with self.subTest(text=text):
                result = verify(problem(), work(text))
                self.assertEqual(result["judgement"], "undetermined")

    def test_confirmed_raw_input_is_preserved_and_not_mutated(self):
        p, w = problem(), work(" 2（x+3）＝10 ", "x=2")
        snapshot = deepcopy((p, w))
        result = verify(p, w)
        self.assertEqual((p, w), snapshot)
        self.assertEqual(result["steps"][0]["text"], " 2（x+3）＝10 ")

    def test_supported_variable_is_not_hardcoded(self):
        result = verify(problem("3y=6", variable="y"), work("y=2"))
        self.assertEqual((result["judgement"], result["completion"]), ("correct", "complete"))


if __name__ == "__main__":
    unittest.main()
