# 관찰된 식 변화로 재확인 안내와 후속 모델 작업 계획

# 현재 입력과 지문이 일치하는 새 검증 결과를 사용한다. 지문은 승인 증거가 아님
# 좌변·우변 근거는 관찰된 식의 차이이며 학생의 의도·오개념을 설명하지 않음
# route는 호출 계획일 뿐 모델을 실행하거나 자유 힌트 공개를 승인하지 않음
# 학생 출력은 고정 템플릿이며 입력 변경·외부 호출은 하지 않음
# 공통 입출력과 full/compact 문맥 계약은 solution_service.py를 따름

import re

from . import feedback_policy as previous_policy
from . import step_verifier as verifier


GUIDED_POLICY_VERSION = "guided-feedback-v1.0.1"
_DISTRIBUTIVE_PRODUCT = re.compile(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+|\([+-]?\d+/[+-]?\d+\)))\*?\(([^()]+)\)")
SIDE_HINTS = {
    "lhs": "표시된 줄의 왼쪽 식이 변형 전과 같은 의미인지 다시 확인해 보세요.",
    "rhs": "표시된 줄의 오른쪽 식이 변형 전과 같은 의미인지 다시 확인해 보세요.",
    "equation": "표시된 줄의 양변이 같은 조건을 유지하도록 바뀌었는지 앞 식과 비교해 보세요.",
}


def _validate_pair(problem, work, result):
    """현재 문제·풀이·버전과 결과의 입력 지문이 일치하는지 확인한다."""
    verifier._validate(problem, work)
    previous_policy._validate_result(result)
    if (result.get("verifier_version") != verifier.VERIFIER_VERSION
            or result.get("problem_id") != problem["id"]
            or result.get("problem_revision") != problem["revision"]
            or result.get("input_fingerprint") != verifier.input_fingerprint(problem, work)):
        raise ValueError("현재 문제 버전의 검증 결과가 필요합니다.")
    if len(result["steps"]) != len(work["steps"]):
        raise ValueError("검증 결과와 관찰한 학생 풀이가 다릅니다.")
    for row, step in zip(result["steps"], work["steps"]):
        if any(row.get(key) != step[field] for key, field in
               (("step_id", "id"), ("text", "text"), ("state", "state"))):
            raise ValueError("검증 결과와 관찰한 학생 풀이가 다릅니다.")


def _sides(text, variable):
    """지원 제한을 적용한 좌변·우변의 선형 계수·상수쌍을 반환한다."""
    verifier._equation(text, variable)
    parser = verifier._Parser(verifier._tokens(text, variable), variable)
    left = parser.expression()
    if parser.current() != "=":
        raise ValueError("등식 표기를 확인해 주세요.")
    parser.index += 1
    right = parser.expression()
    if parser.current() is not None:
        raise ValueError("등식 표기를 끝까지 읽지 못했습니다.")
    return ((left.coefficient, left.constant), (right.coefficient, right.constant))


def distribution_product(text, variable):
    """한쪽 식이 수와 괄호의 단일 곱이면 두 부분을 반환한다."""
    normalized = "".join(token.value for token in verifier._tokens(text, variable))
    product = _DISTRIBUTIVE_PRODUCT.fullmatch(normalized)
    return product.groups() if product else None


def expression_error_evidence(preceding, current, variable):
    """일차식에서 바뀐 항과 정확히 확인한 음수 곱 오류만 기록한다."""
    try:
        before = verifier.expression_parts(preceding, variable)[-1][1]
        after_text, after = next((text, value) for text, value in verifier.expression_parts(current, variable)
                                 if value != before)
    except (ValueError, StopIteration):
        return {}
    evidence = {"target_side": "expression", "reason_code": "linear_expression_changed",
                "coefficient_changed": before[0] != after[0], "constant_changed": before[1] != after[1]}
    if before[0] != after[0] or re.search(r"[()（）]", after_text):
        return evidence
    preceding = preceding.replace("＝", "=").split("=")[-1]
    tokens = [token.value for token in verifier._tokens(verifier.expression_notation(preceding), variable)]
    start, depth, terms = 0, 0, []
    for index, token in enumerate(tokens):
        if depth == 0 and token in ("+", "-") and index and tokens[index - 1] not in "+-*/":
            terms.append("".join(tokens[start:index]))
            start = index
        depth += (token == "(") - (token == ")")
    terms.append("".join(tokens[start:]))
    matches = []
    for term in terms:
        product = distribution_product(term, variable)
        if product is None:
            continue
        factor, inner = product
        scalar = verifier.expression_value(factor, variable)[1]
        inner_constant = verifier.expression_value(inner, variable)[1]
        if scalar < 0 and inner_constant < 0 and before[1] - after[1] == 2 * scalar * inner_constant:
            matches.append({"factor": factor, "constant": str(inner_constant)})
    if len(matches) == 1:
        evidence.update(concept_error="negative_distribution_sign", distribution=matches[0])
    return evidence


def negative_distribution_error(target, response):
    """확인된 첫 오류에서 음수 곱의 상수항만 반대 부호가 됐는지 확인한다."""
    result = response.get("verification") or {}
    evidence = response.get("evidence") or {}
    side_name = evidence.get("target_side")
    if (response.get("tool_supported") is not True or result.get("judgement") != "incorrect"
            or result.get("earliest_unresolved_step_id") is not None):
        return False
    first_error = result.get("first_error_step_id")
    rows = {row["step_id"]: row for row in result.get("steps", [])}
    error = rows.get(first_error)
    if not error or error.get("state") != "active" or error.get("judgement") != "incorrect":
        return False
    if side_name == "expression":
        return evidence.get("concept_error") == "negative_distribution_sign"
    if side_name not in {"lhs", "rhs"}:
        return False
    preceding = rows.get(evidence.get("previous_step_id"), {}).get("text", target)
    letters = set(re.findall(r"[A-Za-z]", target))
    if len(letters) != 1:
        return False
    variable = next(iter(letters))
    side = 0 if side_name == "lhs" else 1
    try:
        before = _sides(preceding, variable)
        after = _sides(error["text"], variable)
        coefficient, constant = before[side]
        if (before[1 - side] != after[1 - side] or not coefficient or constant <= 0
                or after[side] != (coefficient, -constant)):
            return False
        if re.search(r"[()（）]", error["text"].replace("＝", "=").split("=")[side]):
            return False
        product = distribution_product(preceding.replace("＝", "=").split("=")[side], variable)
        if product is None:
            return False
        factor = verifier._Parser(verifier._tokens(product[0], variable), variable).expression().constant
        return factor < 0
    except ValueError:
        return False


def error_evidence(problem: dict, work: dict, result: dict) -> dict:
    """관찰된 식 차이만으로 재확인 대상을 정하며 연산 의도는 추론하지 않는다."""
    _validate_pair(problem, work, result)
    evidence = {
        "schema_version": "error-side-evidence-v1", "target_side": "unknown",
        "step_id": result["first_error_step_id"], "previous_step_id": None,
        "reason_code": "no_confirmed_error", "evidence_basis": {},
        "hint_key": "no_local_error_hint",
    }
    if result["action"] == "unsupported":
        evidence["reason_code"] = "unsupported_scope"
        return evidence
    if result["first_error_step_id"] is None:
        if result["earliest_unresolved_step_id"] is not None:
            evidence["reason_code"] = "first_error_not_confirmed"
        return evidence
    if (problem["confirmed"] is not True or problem["domain"] != "real"
            or problem["profile"] != "linear-equation-v1"
            or work["problem_id"] != problem["id"]
            or work["problem_revision"] != problem["revision"]):
        evidence["reason_code"] = "unconfirmed_source"
        return evidence

    previous_text = problem["equation"]
    origin = "confirmed_problem"
    current = None
    for step, row in zip(work["steps"], result["steps"]):
        if row.get("skipped") is True:
            continue
        if step["id"] == result["first_error_step_id"]:
            if row["judgement"] == "incorrect" and step["confirmed"] is True and step["state"] == "active":
                current = step
            break
        if (row["judgement"] != "correct" or step["confirmed"] is not True
                or step["state"] != "active"):
            evidence["reason_code"] = "previous_observation_unresolved"
            return evidence
        previous_text = step["text"]
        evidence["previous_step_id"] = step["id"]
        origin = "previous_verified_step"
    if current is None:
        evidence["reason_code"] = "error_observation_unavailable"
        return evidence
    try:
        before = _sides(previous_text, problem["variable"])
        after = _sides(current["text"], problem["variable"])
    except (ValueError, verifier._ExpressionIssue):
        evidence.update(target_side="equation", reason_code="local_expression_unavailable",
                        hint_key="recheck_equation_transform")
        return evidence
    same_left, same_right = before[0] == after[0], before[1] == after[1]
    evidence["evidence_basis"] = {
        "comparison": "exact_linear_expression_normal_forms", "origin": origin,
        "lhs_expression_unchanged": same_left, "rhs_expression_unchanged": same_right,
        "operation_intent_inferred": False,
    }
    if same_left != same_right:
        side = "rhs" if same_left else "lhs"
        evidence.update(target_side=side, reason_code="one_side_expression_changed",
                        hint_key="recheck_" + side + "_transform")
    elif not same_left:
        evidence.update(target_side="equation", reason_code="both_sides_changed_no_local_proof",
                        hint_key="recheck_equation_transform")
    else:
        evidence.update(reason_code="no_local_expression_change", hint_key="recheck_equation_transform")
    return evidence


def route(result: dict) -> dict:
    """현재 검증 결과의 후속 모델 작업 계획만 반환하며 호출·공개는 수행하지 않는다."""
    previous_policy._validate_result(result)
    active = [step for step in result["steps"] if step.get("skipped") is not True]
    completed = (
        result["judgement"] == "correct" and result["completion"] == "complete"
        and result["action"] == "continue" and bool(active)
        and result["first_error_step_id"] is None
        and result["first_observed_error_step_id"] is None
        and result["earliest_unresolved_step_id"] is None
        and all(step["judgement"] == "correct" for step in active)
    )
    if completed:
        required, reason = False, "completed_correct"
    elif result["action"] == "unsupported":
        required, reason = False, "unsupported_scope"
    elif result["action"] == "ask_confirmation" or result["earliest_unresolved_step_id"] is not None:
        required, reason = False, "needs_input_confirmation"
    elif result["action"] == "first_hint":
        required, reason = True, "first_hint_requested"
    elif result["judgement"] == "incorrect":
        required, reason = True, "observed_error"
    elif result["judgement"] == "correct":
        required, reason = True, "unfinished_work"
    else:
        required, reason = False, "undetermined_math"
    return {"schema_version": "guided-model-route-v1", "model_required": required, "reason": reason}


def context(problem: dict, work: dict, result: dict, mode="compact") -> dict:
    """full에는 상세 근거를, compact에는 판정·위치·행동 요약만 복사한다."""
    if mode not in ("full", "compact"):
        raise ValueError("context mode는 full 또는 compact여야 합니다.")
    evidence = error_evidence(problem, work, result)
    if mode == "full":
        output = previous_policy.verifier_context(result)
        output["guided_policy_version"] = GUIDED_POLICY_VERSION
        output["error_evidence"] = evidence
        return output
    return {
        "judgement": result["judgement"], "first_error_step_id": result["first_error_step_id"],
        "first_observed_error_step_id": result["first_observed_error_step_id"],
        "earliest_unresolved_step_id": result["earliest_unresolved_step_id"],
        "completion": result["completion"], "action": result["action"],
        "target_side": evidence["target_side"], "hint_key": evidence["hint_key"],
    }


def student_feedback(problem: dict, work: dict, result: dict) -> dict:
    """관찰 근거에 맞는 고정 재확인 안내를 선택한다."""
    evidence = error_evidence(problem, work, result)
    output = previous_policy.student_feedback(result)
    output.update(policy_version=GUIDED_POLICY_VERSION, source="guided_verifier_template",
                  error_target_side=evidence["target_side"])
    if (result["judgement"] == "incorrect" and result["action"] == "continue"
            and result["earliest_unresolved_step_id"] is None):
        side = evidence["target_side"]
        if side in SIDE_HINTS:
            output.update(hint_template_id=evidence["hint_key"], hint=SIDE_HINTS[side])
    return output
