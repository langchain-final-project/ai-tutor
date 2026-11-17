# 확인된 일차방정식과 학생 등식의 해집합을 비교하는 순수 계산 검증기
# 실수 범위의 영문 한 변수·유일해 일차방정식만 Fraction으로 정확히 계산
# 정수·소수·분수·괄호·사칙연산과 명확한 생략 곱셈을 지원
# 도형·자유 서술·비선형식·변수 분모·일반 LaTeX는 지원하지 않음
# 미확인·읽지 못한 줄은 오답으로 단정하지 않고 확인을 요청
# 각 관찰 등식만 비교하므로 생략한 연산·설명·학생의 의도는 증명하지 않음
# 서비스는 매번 새로 검증하는 solution_service.evaluate_reviewed_solution을 사용
# 입력 필드는 아래 상수, 전체 연결 계약은 해당 공개 진입점에서 확인
# confirmed와 revision의 승인·갱신은 상위 서버의 책임

from dataclasses import dataclass
from fractions import Fraction
import hashlib
import json
import re


VERIFIER_VERSION = "linear-equation-v1.0.1"
MAX_TEXT = 512
MAX_TOKENS = 256
MAX_DEPTH = 32
MAX_NUMBER_DIGITS = 32
MAX_FRACTION_BITS = 4096
MAX_STEPS = 100
PROBLEM_FIELDS = {"schema_version", "id", "revision", "equation", "variable", "domain", "confirmed", "profile"}
WORK_FIELDS = {"schema_version", "problem_id", "problem_revision", "steps"}
STEP_FIELDS = {"id", "text", "state", "confirmed"}


class _ExpressionIssue(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class _Token:
    value: str
    implicit: bool = False


@dataclass(frozen=True)
class _Linear:
    """계수가 소거돼도 변수 등장 흔적을 남겨 지원 범위를 보수적으로 검사한다."""

    coefficient: Fraction
    constant: Fraction
    contains_variable: bool = False
    bare_variable: bool = False


def _bounded(value):
    for item in (value.coefficient, value.constant):
        if max(item.numerator.bit_length(), item.denominator.bit_length()) > MAX_FRACTION_BITS:
            raise _ExpressionIssue("expression_limit", "수식 계산 범위를 넘었습니다. 더 짧은 식을 확인해 주세요.")
    return value


def _tokens(text, variable):
    """허용 기호와 명확한 생략 곱셈만 읽고 모호한 표기는 보류한다."""
    # 뜻이 명확한 문자만 치환한다. 원문은 결과에 남기고 틀린 계산은 고치지 않는다.
    translated = text.translate(str.maketrans({"−": "-", "×": "*", "÷": "/", "（": "(", "）": ")", "＝": "="}))
    result, pos = [], 0
    pattern = re.compile(r"(?:\d+(?:\.\d*)?|\.\d+)|[A-Za-z]|[+*/()=\-]")
    while pos < len(translated):
        if translated[pos].isspace():
            pos += 1
            continue
        match = pattern.match(translated, pos)
        if match is None:
            raise _ExpressionIssue("unsupported_notation", "현재 문법으로 읽을 수 없는 기호나 설명이 있습니다.")
        value = match.group()
        if value.isalpha() and value != variable:
            raise _ExpressionIssue("unsupported_variable", "확인된 변수 외의 문자가 있습니다.")
        if value[0].isdigit() or value[0] == ".":
            if sum(c.isdigit() for c in value) > MAX_NUMBER_DIGITS:
                raise _ExpressionIssue("expression_limit", "숫자가 너무 깁니다. 원문을 확인해 주세요.")
        result.append(_Token(value))
        if len(result) > MAX_TOKENS:
            raise _ExpressionIssue("expression_limit", "수식의 기호가 너무 많습니다.")
        pos = match.end()

    expanded = []
    for token in result:
        if expanded:
            previous = expanded[-1].value
            is_number = lambda s: bool(s) and (s[0].isdigit() or s[0] == ".")
            # x1은 첨자 변수일 수도 있으므로 확인 없이 x*1로 읽지 않는다.
            if previous == variable and is_number(token.value):
                raise _ExpressionIssue("ambiguous_notation", "변수 뒤의 숫자 표기는 곱셈인지 확인이 필요합니다.")
            left_factor = is_number(previous) or previous in (variable, ")")
            right_factor = token.value in (variable, "(")
            if left_factor and right_factor:
                expanded.append(_Token("*", implicit=True))
        expanded.append(token)
        if len(expanded) > MAX_TOKENS:
            raise _ExpressionIssue("expression_limit", "수식의 기호가 너무 많습니다.")
    return expanded


class _Parser:
    """입력을 코드로 실행하지 않고 허용 문법만 직접 해석한다."""

    def __init__(self, tokens, variable):
        self.tokens, self.variable, self.index = tokens, variable, 0

    def current(self):
        return self.tokens[self.index].value if self.index < len(self.tokens) else None

    def expression(self, depth=0):
        value = self.product(depth)
        while self.current() in ("+", "-"):
            op = self.current()
            self.index += 1
            other = self.product(depth)
            sign = 1 if op == "+" else -1
            value = _bounded(_Linear(value.coefficient + sign * other.coefficient,
                                     value.constant + sign * other.constant,
                                     value.contains_variable or other.contains_variable))
        return value

    def product(self, depth):
        """곱셈·나눗셈을 계산하되 비선형식·변수 분모·모호한 나눗셈은 보류한다."""
        value = self.factor(depth)
        previous_op = None
        while self.current() in ("*", "/"):
            token = self.tokens[self.index]
            if token.implicit and previous_op == "/":
                raise _ExpressionIssue("ambiguous_division", "나눗셈 뒤의 암묵적 곱셈은 괄호로 확인해 주세요.")
            self.index += 1
            other = self.factor(depth)
            if token.value == "*":
                if value.contains_variable and other.contains_variable:
                    raise _ExpressionIssue("nonlinear_expression", "변수끼리의 곱은 현재 검증 범위 밖입니다.")
                value = _bounded(_Linear(value.coefficient * other.constant + other.coefficient * value.constant,
                                         value.constant * other.constant,
                                         value.contains_variable or other.contains_variable))
            else:
                if other.contains_variable:
                    raise _ExpressionIssue("variable_denominator", "변수가 분모에 있는 식은 현재 검증 범위 밖입니다.")
                if other.constant == 0:
                    raise _ExpressionIssue("division_by_zero", "확인된 식에 0으로 나누는 연산이 있습니다.")
                value = _bounded(_Linear(value.coefficient / other.constant, value.constant / other.constant,
                                         value.contains_variable))
            previous_op = token.value
        return value

    def factor(self, depth):
        if depth > MAX_DEPTH:
            raise _ExpressionIssue("expression_limit", "괄호나 부호의 중첩이 너무 깊습니다.")
        current = self.current()
        if current in ("+", "-"):
            self.index += 1
            inner = self.factor(depth + 1)
            sign = 1 if current == "+" else -1
            return _bounded(_Linear(sign * inner.coefficient, sign * inner.constant,
                                    inner.contains_variable, inner.bare_variable and sign == 1))
        if current == "(":
            self.index += 1
            value = self.expression(depth + 1)
            if self.current() != ")":
                raise _ExpressionIssue("invalid_syntax", "괄호의 짝을 확인해 주세요.")
            self.index += 1
            return value
        if current == self.variable:
            self.index += 1
            return _Linear(Fraction(1), Fraction(0), True, True)
        if current is not None and (current[0].isdigit() or current[0] == "."):
            self.index += 1
            try:
                number = Fraction(current)
            except (ValueError, ZeroDivisionError):
                raise _ExpressionIssue("invalid_syntax", "숫자 표기를 확인해 주세요.") from None
            return _Linear(Fraction(0), number)
        raise _ExpressionIssue("invalid_syntax", "등식의 항이나 연산 기호를 확인해 주세요.")


def _equation(text, variable):
    """등식의 정확한 해와 답 형태를 구한다. root=None이면 유일해가 없다."""
    if len(text) > MAX_TEXT:
        raise _ExpressionIssue("expression_limit", "한 줄의 수식이 너무 깁니다.")
    tokens = _tokens(text, variable)
    if sum(t.value == "=" for t in tokens) != 1:
        raise _ExpressionIssue("invalid_syntax", "한 줄에는 등호가 하나인 식이 필요합니다.")
    parser = _Parser(tokens, variable)
    left = parser.expression()
    if parser.current() != "=":
        raise _ExpressionIssue("invalid_syntax", "등호 앞의 표기를 확인해 주세요.")
    parser.index += 1
    right = parser.expression()
    if parser.current() is not None:
        raise _ExpressionIssue("invalid_syntax", "한 줄의 식을 끝까지 해석할 수 없습니다.")
    coefficient, constant = left.coefficient - right.coefficient, left.constant - right.constant
    root = -constant / coefficient if coefficient else None
    complete = ((left.bare_variable and not right.contains_variable)
                or (right.bare_variable and not left.contains_variable))
    return {"coefficient": coefficient, "constant": constant, "root": root, "complete": complete}


def _mapping(value, fields, label):
    if type(value) is not dict or set(value) != fields:
        raise ValueError(label + " 필드가 계약과 다릅니다.")


def _text(value, label, maximum=255):
    if type(value) is not str or not value.strip() or len(value) > maximum:
        raise ValueError(label + " 값은 길이 제한 안의 비어 있지 않은 문자열이어야 합니다.")


def _validate(problem, work):
    """필드·자료형·줄 ID를 검사한다. 수학적 지원 여부는 verify에서 판단한다."""
    _mapping(problem, PROBLEM_FIELDS, "problem")
    _mapping(work, WORK_FIELDS, "work")
    if problem["schema_version"] != "verification-problem-v1" or work["schema_version"] != "verification-work-v1":
        raise ValueError("지원하지 않는 계약 버전입니다.")
    for name in ("id", "revision", "domain", "profile"):
        _text(problem[name], "problem." + name)
    _text(problem["equation"], "problem.equation", 10000)
    if type(problem["variable"]) is not str or re.fullmatch(r"[A-Za-z]", problem["variable"]) is None:
        raise ValueError("변수는 영문 한 글자여야 합니다.")
    if type(problem["confirmed"]) is not bool:
        raise ValueError("problem.confirmed는 bool이어야 합니다.")
    _text(work["problem_id"], "work.problem_id")
    _text(work["problem_revision"], "work.problem_revision")
    if type(work["steps"]) is not list or len(work["steps"]) > MAX_STEPS:
        raise ValueError("학생 풀이 steps는 100개 이하의 배열이어야 합니다.")
    ids = set()
    for step in work["steps"]:
        _mapping(step, STEP_FIELDS, "step")
        _text(step["id"], "step.id")
        # 글자를 읽지 못한 OCR 영역도 위치를 보존하고 미확인 줄로 다룬다.
        # 빈 문자열을 계약 오류로 버리지 않으며, 확인됐더라도 파서를 통과하지 못한다.
        if type(step["text"]) is not str or len(step["text"]) > 10000:
            raise ValueError("step.text는 10000자 이하의 문자열이어야 합니다.")
        if step["id"] in ids:
            raise ValueError("step.id는 중복될 수 없습니다.")
        ids.add(step["id"])
        if type(step["state"]) is not str or step["state"] not in ("active", "deleted", "uncertain"):
            raise ValueError("step.state는 active/deleted/uncertain 중 하나여야 합니다.")
        if type(step["confirmed"]) is not bool:
            raise ValueError("step.confirmed는 bool이어야 합니다.")


def input_fingerprint(problem: dict, work: dict) -> str:
    """입력 전체와 검증기 버전의 SHA-256 지문을 반환한다.

    승인·인증을 대신하지 않으며 원문·줄 순서·확인 상태를 그대로 반영한다.
    """
    _validate(problem, work)
    payload = {"verifier_version": VERIFIER_VERSION, "problem": problem, "work": work}
    # ASCII 이스케이프로 읽을 수 없는 유니코드도 보존한다. 운영체제별 UTF-8
    # 오류 처리에 의존하지 않으며 원문을 임의로 고치지 않는다.
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=True, allow_nan=False).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def _base(problem, fingerprint):
    return {"schema_version": "verification-result-v1", "verifier_version": VERIFIER_VERSION,
            "problem_id": problem["id"], "problem_revision": problem["revision"],
            "input_fingerprint": fingerprint,
            "judgement": "undetermined", "action": "ask_confirmation",
            "verification_basis": "confirmed_equation_solution_set_in_supported_profile",
            "valid_prefix_until_step_id": None, "first_error_step_id": None,
            "first_observed_error_step_id": None, "earliest_unresolved_step_id": None,
            "completion": "unknown", "steps": []}


def _blocked(result, work, code, reason, action="ask_confirmation"):
    result.update(action=action, reason_code=code, reason=reason)
    result["steps"] = [{"step_id": step["id"], "text": step["text"], "state": step["state"],
                        "judgement": "undetermined", "reason_code": "not_evaluated", "reason": reason,
                        "skipped": step["state"] == "deleted"} for step in work["steps"]]
    return result


def verify(problem: dict, work: dict) -> dict:
    """현재 관찰 등식들을 원래 식의 해집합과 비교한다.

    계약 위반은 ValueError이며 미확인·미지원 수학은 결과 상태로 반환한다.
    completion은 마지막 답 형태이므로 앞선 오류와 complete가 공존할 수 있다.
    first_observed_error_step_id는 관찰된 첫 오류다. first_error_step_id는
    그보다 앞에 미확인 줄이 없어 최초 오류로 확정할 수 있을 때만 채운다.
    """
    fingerprint = input_fingerprint(problem, work)
    result = _base(problem, fingerprint)
    if work["problem_id"] != problem["id"] or work["problem_revision"] != problem["revision"]:
        return _blocked(result, work, "problem_revision_mismatch", "현재 문제와 풀이가 연결된 문제 버전을 확인해 주세요.")
    if not problem["confirmed"]:
        return _blocked(result, work, "problem_unconfirmed", "원래 문제의 식과 조건을 먼저 확인해 주세요.")
    if problem["domain"] != "real" or problem["profile"] != "linear-equation-v1":
        return _blocked(result, work, "unsupported_profile", "현재는 실수 범위의 유일해 일차방정식만 검증합니다.", "unsupported")
    try:
        original = _equation(problem["equation"], problem["variable"])
    except _ExpressionIssue as error:
        return _blocked(result, work, error.code, str(error), "unsupported")
    if original["root"] is None:
        return _blocked(result, work, "nonunique_problem", "해가 없거나 무수히 많은 원문은 현재 검증 범위 밖입니다.", "unsupported")

    prefix_open, previous, first_error_index, unresolved_index = True, None, None, None
    last_active, final_correct, active_count = None, False, 0
    for index, step in enumerate(work["steps"]):
        row = {"step_id": step["id"], "text": step["text"], "state": step["state"],
               "previous_step_id": previous, "judgement": "undetermined", "skipped": False}
        parsed = None
        if step["state"] == "deleted" and step["confirmed"]:
            row.update(skipped=True, reason_code="deleted", reason="삭제한 것으로 확인된 줄은 검증 대상에서 제외합니다.")
            result["steps"].append(row)
            continue
        active_count += 1
        last_active = row
        previous = step["id"]
        if step["state"] == "uncertain" or not step["confirmed"]:
            row.update(reason_code="step_unconfirmed", reason="이 줄의 원문·순서·활성 여부를 먼저 확인해 주세요.")
        else:
            try:
                parsed = _equation(step["text"], problem["variable"])
                if parsed["root"] == original["root"]:
                    row.update(judgement="correct", reason_code="equivalent_solution_set",
                               reason="지원 범위에서 확인된 원래 식과 같은 해집합을 유지합니다. 생략한 연산은 추측하지 않습니다.")
                else:
                    row.update(judgement="incorrect", reason_code="solution_set_changed",
                               reason="이 줄의 식은 확인된 원래 식과 해집합이 다릅니다.")
            except _ExpressionIssue as error:
                row.update(reason_code=error.code, reason=str(error))
                if error.code == "division_by_zero":
                    row["judgement"] = "incorrect"
        if row["judgement"] == "incorrect" and first_error_index is None:
            first_error_index = index
            result["first_observed_error_step_id"] = step["id"]
        if row["judgement"] == "undetermined" and unresolved_index is None:
            unresolved_index = index
            result["earliest_unresolved_step_id"] = step["id"]
        if prefix_open and row["judgement"] == "correct":
            result["valid_prefix_until_step_id"] = step["id"]
        else:
            prefix_open = False
        final_correct = bool(parsed and parsed["complete"] and row["judgement"] == "correct")
        result["steps"].append(row)

    if active_count == 0:
        result.update(action="first_hint", completion="no_work", reason_code="no_active_work",
                      reason="확인할 활성 풀이가 없습니다. 첫 힌트 흐름으로 진행합니다.")
        return result
    if first_error_index is not None:
        result["judgement"] = "incorrect"
        if unresolved_index is None or first_error_index < unresolved_index:
            result["first_error_step_id"] = result["first_observed_error_step_id"]
    elif unresolved_index is None:
        result["judgement"] = "correct"
    result["action"] = "ask_confirmation" if unresolved_index is not None else "continue"
    result["completion"] = ("complete" if final_correct else "unknown"
                            if last_active["judgement"] == "undetermined" else "incomplete")
    result["reason_code"] = ("observed_error" if first_error_index is not None else
                             "needs_confirmation" if unresolved_index is not None else "verified_observed_steps")
    result["reason"] = "관찰된 식의 타당성과 최종 답의 완료 여부를 구분한 결과입니다."
    return result
