"""입력 부족·상태 충돌·후보 중복이 최종 유형 확정으로 넘어가지 않는지 검사한다."""

import unittest

from app.tutor.classification_contract import (
    ClassificationRequest, TableInput, TypeCandidate,
    classification_json_schema, validate_decision,
)


def request(**changes):
    return {
        "question_text": "방정식 2x+1=5의 해를 구하여라.",
        "equations": ["2x+1=5"], "grade": 1, "user_confirmed": True,
        "has_figure": False, "has_table": False, **changes,
    }


def classified(**changes):
    return {
        "status": "classified", "source_topic_code": "7103047",
        "source_topic_name": "간단한 일차방정식의 풀이", "method": "model",
        "reasons": ["해를 구하는 일차방정식으로 판단"], **changes,
    }


def candidate(**changes):
    return {
        "source_topic_code": "7103047", "source_topic_name": "간단한 일차방정식의 풀이",
        "score": 2.4, "score_name": "decision_function", "reasons": ["모델의 유형 후보"], **changes,
    }


class ClassificationContractTests(unittest.TestCase):
    def test_type_can_be_classified_without_same_question(self):
        result = validate_decision(request(), classified())
        self.assertEqual(result.status, "classified")
        self.assertIsNone(result.matched_question_id)

    def test_exact_match_requires_a_question_id(self):
        with self.assertRaises(ValueError):
            validate_decision(request(), classified(method="exact_match"))

    def test_unconfirmed_or_missing_information_cannot_be_classified(self):
        for changes in [
            {"user_confirmed": False}, {"has_figure": None}, {"has_table": None},
            {"has_figure": True}, {"has_table": True}, {"grade": None},
            {"missing_information": ["conditions"]},
            {"has_figure": True, "figures": [{"id": "diagram", "conditions": ["BC=6cm"],
                                              "unreadable_parts": ["점 A의 각도"]}]},
        ]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_decision(request(**changes), classified())

    def test_partial_input_can_request_specific_clarification(self):
        result = validate_decision(request(has_figure=True), {
            "status": "needs_clarification", "reasons": ["도형의 조건 누락"],
            "questions": [{"field": "figures", "question": "그림의 각도와 변 길이를 확인해 주세요."}],
        })
        self.assertEqual(result.questions[0].field, "figures")
        self.assertNotIn("source_topic_code", result.model_dump())

    def test_all_middle_school_grades_are_supported(self):
        for grade, code, name, text in [
            (1, "7103047", "간단한 일차방정식의 풀이", "방정식 2x+1=5를 풀어라."),
            (2, "8101016", "순환소수를 두 수의 곱의 꼴로 나타내기", "순환소수로 나타내어라."),
            (3, "9101020", "제곱하여 미지수의 값 구하기", "제곱근의 값으로 a를 구하여라."),
        ]:
            with self.subTest(grade=grade):
                result = validate_decision(request(grade=grade, question_text=text, equations=[]),
                                           classified(source_topic_code=code, source_topic_name=name))
                self.assertEqual(result.status, "classified")

    def test_omitted_grade_is_not_assumed_to_be_middle1(self):
        payload = request()
        payload.pop("grade")
        self.assertIsNone(ClassificationRequest.model_validate(payload).grade)
        with self.assertRaises(ValueError):
            validate_decision(payload, classified())

    def test_undecided_status_cannot_carry_selected_type(self):
        with self.assertRaises(ValueError):
            validate_decision(request(), {
                "status": "candidates", "candidates": [candidate()], "reasons": ["유형 검토 중"],
                "source_topic_code": "7103047",
            })

    def test_candidates_are_unique_types_not_duplicate_questions(self):
        with self.assertRaises(ValueError):
            validate_decision(request(), {
                "status": "candidates", "candidates": [candidate(), candidate()],
                "reasons": ["검토 중"],
            })

    def test_selected_type_agrees_with_final_candidate_order(self):
        with self.assertRaises(ValueError):
            validate_decision(request(), classified(candidates=[candidate(source_topic_code="7103046")]))

    def test_model_score_is_not_restricted_to_probability_range(self):
        self.assertEqual(TypeCandidate.model_validate(candidate()).score, 2.4)
        self.assertEqual(TypeCandidate.model_validate(candidate(score=-1.3)).score, -1.3)
        for changes in [{"score": float("nan")}, {"score": float("inf")}, {"score_name": None}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                TypeCandidate.model_validate(candidate(**changes))

    def test_math_and_intentional_table_blanks_are_preserved(self):
        equations = [r"-5^2", r"(-5)^2", r"\frac{3x+1}{2}=0.5"]
        value = ClassificationRequest.model_validate(request(equations=equations))
        self.assertEqual(value.equations, equations)
        table = TableInput(id="table", rows=[["계급", "도수"], ["70~80", ""]])
        self.assertEqual(table.rows[1][1], "")
        with self.assertRaises(ValueError):
            TableInput(id="table", rows=[["계급", "도수"], ["70~80"]])

    def test_contradictory_presence_and_unexpected_solution_fields_are_rejected(self):
        for changes in [
            {"figures": [{"id": "figure", "description": "삼각형"}]},
            {"student_solution": "x=2"}, {"grade": True},
        ]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                ClassificationRequest.model_validate(request(**changes))

    def test_json_contract_round_trip_and_schema(self):
        value = validate_decision(request(), classified(candidates=[candidate()]))
        restored = validate_decision(request(), value.model_dump(mode="json"))
        self.assertEqual(value, restored)
        schema = classification_json_schema()
        self.assertEqual(set(schema), {"input", "output"})
        self.assertEqual(set(schema["output"]["discriminator"]["mapping"]), {
            "classified", "candidates", "needs_clarification", "unsupported",
        })


if __name__ == "__main__":
    unittest.main()
