"""학생 확인 전에 재인식 필요성과 최종 전사 후보를 선택한다."""

import re

from .checks import text_issues


UNREADABLE = re.compile(r"\[UNREADABLE\]|\[판독불가\]", re.IGNORECASE)
MAX_ATTEMPTS = 2
OCR_BUDGET_SECONDS = 240


def assess_result(result: dict) -> dict:
    text = result.get("text", "")
    regions = result.get("regions", [])
    unreadable = len(UNREADABLE.findall(text))
    remaining = UNREADABLE.sub("", text).strip(" \n\t.,:;!?$\\()[]{}")
    usable = (
        result.get("status") == "needs_review"
        and bool(remaining)
        and all(region.get("finish_reason") == "stop" for region in regions)
    )
    syntax = [issue for issue in text_issues(text) if "괄호" in issue]
    if (text.count(r"\(") != text.count(r"\)") or
            text.count(r"\[") != text.count(r"\]") or
            len(re.findall(r"(?<!\\)\$", text)) % 2):
        syntax.append("수식 구분 기호가 닫히지 않았습니다.")
    choices = [
        "①②③④⑤".index(match.group()) + 1 if match.group() in "①②③④⑤"
        else int(match.group(1))
        for match in re.finditer(r"[①②③④⑤]|\\textcircled\s*\{([1-5])\}", text)
    ]
    missing = sorted(set(range(min(choices), max(choices) + 1)) - set(choices)) if choices else []
    reasons = []
    finishes = {region.get("finish_reason") for region in regions}
    if "length" in finishes:
        reasons.append("output_truncated")
    if finishes & {"limit", "repetition", "incomplete"}:
        reasons.append("incomplete_output")
    if "error" in finishes or result.get("error_kind") == "transport":
        reasons.append("transport_error")
    if not remaining:
        reasons.append("empty_text")
    if unreadable:
        reasons.append("unreadable")
    if syntax:
        reasons.append("math_syntax")
    if missing:
        reasons.append("choice_gaps")
    if not usable and not reasons:
        reasons.append("incomplete_result")
    if result.get("retryable_error") is False or any(
        region.get("retryable_error") is False for region in regions
    ):
        reasons = []
    warnings = []
    if unreadable:
        warnings.append({"code": "unreadable", "message": "판독하지 못한 부분을 원본과 비교해 수정해 주세요."})
    if syntax:
        warnings.append({"code": "math_syntax", "message": "수식의 괄호와 구분 기호를 원본과 비교해 주세요."})
    if missing:
        warnings.append({"code": "choice_gaps", "message": "선택지 번호 사이에 빠진 항목이 있는지 확인해 주세요."})
    assembly = result.get("region_assembly") or {}
    for warning in assembly.get("warnings", []):
        if warning not in warnings:
            warnings.append(warning)
        if warning["code"] in {"formula_conflict", "formula_alignment"}:
            reasons.append(warning["code"])
    return {
        "usable": usable,
        "retry_reasons": list(dict.fromkeys(reasons)),
        "unreadable_count": unreadable,
        "syntax_issue_count": len(syntax),
        "missing_choice_labels": missing,
        "assembly_issue_count": len(assembly.get("warnings", [])),
        "review_warnings": warnings,
    }


def critical_signature(text: str) -> tuple:
    text = text.translate(str.maketrans({"−": "-", "‒": "-", "²": "^2", "³": "^3"}))
    text = re.sub(r"\\textcircled\s*\{[1-5]\}|[①②③④⑤]", "", text)
    text = re.sub(r"(?m)^\s*\([1-5]\)", "", text)
    for source, target in ((r"\leq", "≤"), (r"\le", "≤"), (r"\geq", "≥"),
                           (r"\ge", "≥"), (r"\neq", "≠"), (r"\times", "*"), (r"\div", "/")):
        text = text.replace(source, target)
    tokens = re.findall(r"\\(?:frac|sqrt)|\d+(?:\.\d+)?|(?<![A-Za-z])[A-Za-z](?![A-Za-z])|[+*/=<>≤≥≠^\-]", text)
    negative = re.findall(r"옳지않|아닌|않은", re.sub(r"\s", "", text))
    return tuple(tokens), tuple(negative)


def select_result(results: list[dict], verified_index: int | None = None) -> tuple[int, dict]:
    assessments = [assess_result(result) for result in results]
    usable = [index for index, assessment in enumerate(assessments) if assessment["usable"]]
    if not usable:
        selected, reason = len(results) - 1, "no_usable_candidate"
    elif len(usable) == 1:
        selected = usable[0]
        reason = "retry_recovered" if selected else "first_usable"
    elif critical_signature(results[0]["text"]) != critical_signature(results[1]["text"]):
        selected = verified_index if verified_index in usable else 0
        reason = "source_verified" if verified_index in usable else "unresolved_critical_difference"
    else:
        selected = min(usable, key=lambda index: (
            len(assessments[index]["missing_choice_labels"]),
            assessments[index]["unreadable_count"],
            assessments[index]["syntax_issue_count"], assessments[index]["assembly_issue_count"], index,
        ))
        reason = "fewer_checks" if selected else "first_on_tie"
    warnings = list(assessments[selected]["review_warnings"])
    if reason == "unresolved_critical_difference":
        warnings.append({
            "code": "critical_mismatch",
            "message": "두 판독의 숫자·부호·수식 또는 부정 표현이 다릅니다. 원본을 보고 확인해 주세요.",
            "first_text": results[0]["text"],
            "second_text": results[1]["text"],
        })
    return selected, {
        "attempt_count": len(results), "selected_attempt": selected + 1,
        "selection_reason": reason, "review_warnings": warnings,
    }
