# 검증 결과에서 학생용 고정 안내와 모델용 제한 문맥 생성

# 수학 판정은 step_verifier가 담당하며 현재 입력을 새로 검증한 결과만 받음
# 이 파일은 결과 형식을 검사하고 고정 템플릿을 선택하며 자유 힌트를 승인하지 않음
# 입력을 변경하거나 모델·파일·네트워크를 호출하지 않음
# 공통 입출력과 확인 책임은 solution_service.py의 계약을 따름

POLICY_VERSION = "feedback-policy-v1.0.1"

JUDGEMENTS = ("correct", "incorrect", "undetermined")
TEMPLATES = {
    "unsupported": "이 풀이를 자동으로 확인할 수 있는 범위를 벗어났어요. 문제와 풀이를 함께 확인해 주세요.",
    "first_hint": "문제에서 주어진 식과 구할 것을 먼저 찾아보세요.",
    "clarify": "표시된 줄의 기호와 활성·삭제 여부를 원본과 다시 확인해 주세요.",
    "check_equivalence": "표시된 줄이 원래 문제의 식과 같은 조건을 유지하는지 다시 살펴보세요.",
    "verify_submitted": "적어 둔 값을 원래 식에 대입해 양변이 같은지 확인해 보세요.",
    "continue": "현재까지 확인한 식을 바탕으로 미지수를 한쪽에 모으는 방법을 생각해 보세요.",
}


def _validate_result(result):
    """결과 구조만 검사하며 수학을 재계산하거나 출처를 인증하지 않는다."""
    if not isinstance(result, dict) or result.get("schema_version") != "verification-result-v1":
        raise ValueError("verification-result-v1 검증 결과가 필요합니다.")
    if result.get("judgement") not in JUDGEMENTS:
        raise ValueError("검증기의 수학 판정이 올바르지 않습니다.")
    if result.get("completion") not in ("complete", "incomplete", "unknown", "no_work"):
        raise ValueError("검증기의 완료 상태가 올바르지 않습니다.")
    if result.get("action") not in ("continue", "ask_confirmation", "unsupported", "first_hint"):
        raise ValueError("검증기의 다음 행동이 올바르지 않습니다.")
    steps = result.get("steps")
    if type(steps) is not list:
        raise ValueError("검증기의 단계 목록이 필요합니다.")
    identifiers = set()
    for step in steps:
        if (not isinstance(step, dict) or not isinstance(step.get("step_id"), str)
                or not step["step_id"] or step["step_id"] in identifiers
                or step.get("judgement") not in JUDGEMENTS
                or not isinstance(step.get("reason_code"), str)):
            raise ValueError("검증기의 단계 기록이 올바르지 않습니다.")
        identifiers.add(step["step_id"])
    for key in ("first_error_step_id", "first_observed_error_step_id", "earliest_unresolved_step_id"):
        if key not in result or (result[key] is not None and result[key] not in identifiers):
            raise ValueError("검증기 오류·미확인 위치가 올바르지 않습니다.")
    return identifiers


def verifier_context(result: dict) -> dict:
    """허용된 검증 사실만 복사하고 참조 답안·해는 제외한다."""
    _validate_result(result)
    output = {"schema_version": "verifier-context-v1", "policy_version": POLICY_VERSION}
    for key in ("verifier_version", "judgement", "first_error_step_id",
                "first_observed_error_step_id", "earliest_unresolved_step_id",
                "completion", "action"):
        output[key] = result.get(key)
    output["steps"] = [{key: step[key] for key in ("step_id", "judgement", "reason_code")}
                       for step in result["steps"]]
    return output


def student_feedback(result: dict) -> dict:
    """검증기 판정과 고정 안내만 반환한다."""
    _validate_result(result)
    if result["action"] == "unsupported":
        template = "unsupported"
    elif result["action"] == "first_hint":
        template = "first_hint"
    elif result["action"] == "ask_confirmation" or result["earliest_unresolved_step_id"] is not None:
        template = "clarify"
    elif result["judgement"] == "incorrect":
        template = "check_equivalence"
    elif result["judgement"] == "correct" and result["completion"] == "complete":
        template = "verify_submitted"
    elif result["judgement"] == "correct":
        template = "continue"
    else:
        template = "clarify"
    return {
        "schema_version": "student-feedback-v1", "policy_version": POLICY_VERSION,
        "source": "verifier_template", "judgement": result["judgement"],
        "first_error_step_id": result["first_error_step_id"],
        "first_observed_error_step_id": result["first_observed_error_step_id"],
        "earliest_unresolved_step_id": result["earliest_unresolved_step_id"],
        "completion": result["completion"], "action": result["action"],
        "hint_template_id": template, "hint": TEMPLATES[template],
    }
