"""학습 상태와 풀이 근거에 따라 다음 도움을 선택하는 공통 튜터 흐름."""

from copy import deepcopy
from functools import lru_cache
import hashlib
import json
import re
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from . import guided_feedback, step_verifier
from .session_model import OllamaTutorClient, TutorModelError, _student_message


POLICY_VERSION = "didgo-tutor-policy-v1"


def initial_state():
    return {
        "goal_concept": None, "goal_concept_source": "unassigned", "goal_concept_is_provisional": True,
        "attempts": 0, "help_level": 0, "max_help_level": 0, "hint_count": 0,
        "answer_revealed": False, "assisted": False, "solved": False,
        "independent_verified": False, "needs_independent_check": False,
        "learning_status": "not_started", "last_steps": [], "last_student_text": "",
        "last_work_fingerprint": None, "last_error_signature": None, "consecutive_errors": 0,
    }


class Turn(TypedDict, total=False):
    problem: dict
    state: dict
    events: list
    request: dict
    model_client: object
    observation: dict
    response: dict
    context: dict
    needs_model: bool


def _generic_verification(reason="unsupported_scope"):
    return {"judgement": "undetermined", "completion": "unknown", "action": "unsupported",
            "reason_code": reason, "first_error_step_id": None,
            "earliest_unresolved_step_id": None, "steps": []}


def _linear_text(coefficient, constant, variable):
    """정확한 Fraction 값을 기존 검증기가 읽을 수 있는 평문 일차식으로 쓴다."""
    if not coefficient:
        return str(constant)
    magnitude = abs(coefficient)
    if magnitude == 1:
        term = variable
    elif magnitude.denominator == 1:
        term = f"{magnitude}{variable}"
    else:
        term = f"({magnitude})*{variable}"
    output = ("-" if coefficient < 0 else "") + term
    if constant:
        output += ("+" if constant > 0 else "-") + str(abs(constant))
    return output


def _verified_scaffold(observed):
    """양변에 문자가 남은 올바른 등식에서 한 번의 같은 연산만 시연한다."""
    result, source = observed["verification"], observed["source"]
    if (not observed["tool_supported"] or result["judgement"] != "correct"
            or result["completion"] != "incomplete" or source is None):
        return None
    active = [step for step in observed["steps"] if step["state"] == "active" and step["text"].strip()]
    if not active:
        return None
    variable = source["variable"]
    try:
        (left_coefficient, left_constant), (right_coefficient, right_constant) = guided_feedback._sides(active[-1]["text"], variable)
        if not left_coefficient or not right_coefficient:
            return None
        equation = (_linear_text(0, left_constant, variable) + "="
                    + _linear_text(right_coefficient - left_coefficient, right_constant, variable))
        checked = step_verifier.verify(source, {
            "schema_version": "verification-work-v1", "problem_id": source["id"],
            "problem_revision": source["revision"], "steps": [
                {"id": "verified-scaffold", "text": equation, "state": "active", "confirmed": True}
            ],
        })
    except ValueError:
        return None
    # 답 형태가 되는 변형은 힌트로 공개하지 않는다.
    if checked["judgement"] != "correct" or checked["completion"] != "incomplete":
        return None
    term = _linear_text(abs(left_coefficient), 0, variable)
    operation = "빼면" if left_coefficient > 0 else "더하면"
    particle = "에서" if left_coefficient > 0 else "에"
    return (f"양변{particle} {term}를 {operation} {equation}이 돼요. "
            f"이제 {variable}를 혼자 남기려면 양변에 어떤 같은 계산을 할까요?")


def _evidence(turn):
    request, problem, state = turn["request"], turn["problem"], turn["state"]
    steps = deepcopy(request.get("steps", []) if request["intent"] == "submit"
                     else request.get("steps") or state.get("last_steps", []))
    student_text = request.get("student_text", "").strip()
    # 평문 등식만 수학 관찰로 옮기고, 모르겠다는 말이나 자유 설명은 등식으로 취급하지 않는다.
    inline_equation = ("=" in student_text or "＝" in student_text) and re.fullmatch(
        r"[\dA-Za-z\s+*/()=\.\-−×÷（）＝]+", student_text,
    )
    if not steps and request["intent"] == "submit" and inline_equation:
        steps = [{"id": "response", "text": student_text, "state": "active"}]
    target = problem["target_text"].strip()
    letters = set(re.findall(r"[A-Za-z]", target))
    verification = _generic_verification()
    evidence, source, observed, solution = {}, None, None, None
    supported = False
    if problem.get("subject", "mathematics") == "mathematics" and len(letters) == 1 and len(target) <= 512:
        source = {
            "schema_version": "verification-problem-v1", "id": str(problem.get("id", "tutor")),
            "revision": str(problem.get("input_version_number", "1")), "equation": target,
            "variable": next(iter(letters)), "domain": "real", "confirmed": True,
            "profile": "linear-equation-v1",
        }
        observed = {"schema_version": "verification-work-v1", "problem_id": source["id"],
                    "problem_revision": source["revision"], "steps": [
                        dict(step, confirmed=step["state"] != "uncertain") for step in steps
                    ]}
        try:
            verification = step_verifier.verify(source, observed)
            supported = verification["action"] != "unsupported"
            if supported:
                evidence = guided_feedback.error_evidence(source, observed, verification)
                solution = step_verifier._equation(target, source["variable"])["root"]
        except ValueError:
            verification = _generic_verification("unsupported_observation")
    uncertain = any(step["state"] == "uncertain" for step in steps)
    if uncertain and not supported:
        verification.update(action="ask_confirmation", reason_code="uncertain_step",
                            earliest_unresolved_step_id=next(step["id"] for step in steps if step["state"] == "uncertain"))
    fingerprint = hashlib.sha256(json.dumps({"steps": steps, "text": student_text},
                                ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return {"observation": {"steps": steps, "student_text": student_text, "verification": verification,
                            "evidence": evidence, "tool_supported": supported, "source": source,
                            "solution": solution, "fingerprint": fingerprint,
                            "help_comment": bool(re.search(r"모르겠|힌트|어떻게|막혔|도와", student_text)) and not inline_equation}}


def _policy(turn):
    request, previous, observed = turn["request"], turn["state"], turn["observation"]
    state = deepcopy(previous)
    verification = observed["verification"]
    active = [step for step in observed["steps"] if step["state"] != "deleted" and step["text"].strip()]
    new_attempt = (request["intent"] == "submit" and bool(active or observed["student_text"])
                   and not (observed["help_comment"] and not request.get("steps"))
                   and observed["fingerprint"] != state.get("last_work_fingerprint"))
    if new_attempt:
        state["attempts"] += 1
        state["last_work_fingerprint"] = observed["fingerprint"]
    if request.get("steps") or request["intent"] == "submit":
        state["last_steps"] = deepcopy(observed["steps"])
        state["last_student_text"] = observed["student_text"]
    completed = (observed["tool_supported"] and verification["judgement"] == "correct"
                 and verification["completion"] == "complete")
    state["solved"] = completed
    state["independent_verified"] = False
    response_source = "policy"
    if turn["problem"].get("needs_confirmation") or verification["action"] == "ask_confirmation":
        action, reason = "ask_confirmation", "unconfirmed_input"
        message = "표시된 문제 조건이나 풀이 기호를 먼저 확인해 주세요. 읽기 불확실한 부분을 오답으로 판단하지 않았어요."
        needs_model = False
    elif request["intent"] == "answer":
        action, reason, message, needs_model = "answer", "explicit_answer_request", "", True
        state.update(answer_revealed=True, assisted=True, help_level=3, needs_independent_check=True)
    elif completed:
        action, reason, needs_model = "complete", "verified_current_work", False
        message = "입력한 활성 풀이와 마지막 답이 확인한 등식의 해를 유지해요. 다른 문제를 도움 없이 푸는 독립 확인은 별도로 진행해요."
        state.update(help_level=0, needs_independent_check=True)
    elif request["intent"] == "more_help":
        action, reason, message, needs_model = "scaffold", "additional_help_requested", "", True
        state["help_level"] = min(2, max(1, state["help_level"] + 1))
        verified_help = _verified_scaffold(observed)
        if verified_help is not None:
            message, reason, needs_model = verified_help, "verified_one_step_scaffold", False
            response_source = "verified_scaffold"
    elif verification["judgement"] == "incorrect":
        signature = str(observed["evidence"].get("target_side", "unknown"))
        if new_attempt:
            state["consecutive_errors"] = state["consecutive_errors"] + 1 if signature == state.get("last_error_signature") else 1
            state["last_error_signature"] = signature
        repeated = state["consecutive_errors"] >= 2
        action, reason, message, needs_model = ("scaffold" if repeated else "error_hint"), "verified_error", "", True
        state["help_level"] = 2 if repeated else max(1, min(2, state["help_level"]))
    elif verification["judgement"] == "correct" and active:
        action, reason, message, needs_model = "next_step", "correct_incomplete_work", "", True
        state.update(help_level=1, consecutive_errors=0, last_error_signature=None)
    else:
        action = "first_hint" if not active and not observed["student_text"] else "guide"
        reason, message, needs_model = ("no_work" if action == "first_hint" else "model_guidance_without_tool_proof"), "", True
        state["help_level"] = max(1, min(2, state["help_level"]))
    if (needs_model or response_source == "verified_scaffold") and action != "answer":
        state["hint_count"] += 1
        state["assisted"] = True
    state["max_help_level"] = max(state["max_help_level"], state["help_level"])
    if completed:
        state["learning_status"] = "solved_with_help" if state["assisted"] else "solved_without_recorded_help"
    elif action == "answer":
        state["learning_status"] = "answer_viewed"
    elif action == "ask_confirmation":
        state["learning_status"] = "needs_confirmation"
    else:
        state["learning_status"] = "in_progress"
    history = []
    for event in turn["events"][-6:]:
        response = event.get("response") or {}
        event_request = event.get("request") or {}
        history.append({"student_text": str(event.get("student_text", event_request.get("student_text", "")))[:1500],
                        "action": response.get("action"), "message": str(response.get("message", ""))[:2000]})
    # 수정된 풀이가 확인됐으면 예전 오답 문구를 모델에 다시 주지 않는다.
    # 과거 도움 이력은 상태에 남기되 현재 정오 판단의 근거는 새 풀이로 한정한다.
    if action == "next_step":
        history = [{"action": item["action"], "status": "historical_help_not_current_work"} for item in history]
    elif action == "answer":
        # 이전 소크라테스식 힌트의 문체가 명시적인 해설 요청을 덮어쓰지 않도록 분리한다.
        history = []
    context = {
        "selected_action": action, "reason": reason,
        "student_text": observed["student_text"], "steps": observed["steps"],
        "verification": verification, "error_evidence": observed["evidence"],
        "tool_supported": observed["tool_supported"],
        "learning_state": {key: state.get(key) for key in (
            "attempts", "help_level", "hint_count", "answer_revealed", "assisted",
            "goal_concept", "goal_concept_source", "goal_concept_is_provisional",
        )},
        "recent_dialogue": history,
    }
    if action == "next_step":
        context["current_work_status"] = "verified_correct_incomplete"
        context["historical_error_status"] = "superseded_by_current_verified_work"
    # 정답 공개를 요청했을 때만 입력에서 직접 계산한 해를 모델의 해설 근거로 제공한다.
    if action == "answer" and observed["solution"] is not None:
        context["verified_solution"] = {"variable": observed["source"]["variable"], "value": str(observed["solution"])}
    if action == "answer":
        context["required_output"] = ("final_answer_first_then_worked_explanation_and_check"
                                      if observed["solution"] is not None
                                      else "final_answer_first_then_worked_explanation")
    return {"state": state, "context": context, "needs_model": needs_model,
            "response": {"action": action, "reason": reason, "policy_version": POLICY_VERSION,
                         "message": message, "verification": verification,
                         "tool_supported": observed["tool_supported"], "evidence": observed["evidence"],
                         "model": None, "usage": {}, "source": response_source}}


def _generate(turn):
    client = turn.get("model_client") or OllamaTutorClient()
    response = dict(turn["response"])
    generated = client.generate(turn["problem"], turn["context"], response["action"])
    message = generated.get("message", "")
    if not isinstance(message, str) or not message.strip():
        raise TutorModelError("학생에게 보여줄 튜터 답변을 받지 못했어요.")
    message = _student_message(message)
    if not message or len(message) > 16000:
        raise TutorModelError("학생에게 보여줄 튜터 답변을 받지 못했어요.")
    response.update(message=message, model=generated.get("model"), usage=generated.get("usage", {}), source="model")
    observed = turn["observation"]
    if response["action"] == "next_step" and observed["tool_supported"] and observed["verification"]["judgement"] == "correct":
        normalized_message = re.sub(r"\\(?:left|right)|[\s\\${}]", "", message).replace("−", "-")
        current_texts = {re.sub(r"\s+", "", step["text"]).replace("−", "-") for step in observed["steps"]}
        old_errors = []
        for event in turn["events"]:
            previous_verification = (event.get("response") or {}).get("verification") or {}
            for step in previous_verification.get("steps", []):
                text = re.sub(r"\s+", "", step.get("text", "")).replace("−", "-")
                if step.get("judgement") == "incorrect" and text and text not in current_texts:
                    old_errors.append(text)
        contradicts_current = bool(re.search(r"틀|잘못|오류|다시\s*계산|맞지\s*않|옳지\s*않", message))
        repeats_old_error = any(text in normalized_message for text in old_errors)
        if contradicts_current or repeats_old_error:
            variable = observed["source"]["variable"]
            response.update(
                message=f"여기까지 입력한 식은 확인됐어요. {variable}를 혼자 남기려면 다음으로 양변에 어떤 같은 계산을 할 수 있을까요?",
                source="guarded_model", output_guard="current_verified_work_preserved",
            )
    # 일차방정식의 최종 해를 직접 공개한 힌트는 공개하지 않고 안전한 질문으로 바꾼다.
    if response["action"] != "answer" and observed["solution"] is not None:
        variable = observed["source"]["variable"]
        terminal = re.findall(r"\b" + re.escape(variable) + r"\s*=\s*([+\-−]?\s*\d+(?:\.\d+)?(?:\s*/\s*\d+)?)", response["message"])
        from fractions import Fraction
        leaked = False
        for value in terminal:
            try:
                leaked = leaked or Fraction(value.replace("−", "-").replace(" ", "")) == observed["solution"]
            except (ValueError, ZeroDivisionError):
                pass
        if leaked:
            side = observed["evidence"].get("target_side")
            response["message"] = ("표시된 줄의 " + ("왼쪽" if side == "lhs" else "오른쪽") + " 식이 앞 식과 같은 의미인지 확인해 볼까요?"
                                   if side in ("lhs", "rhs") else "현재 식에서 어떤 항을 먼저 정리할 수 있을까요? 한 단계만 적어 주세요.")
            response.update(source="guarded_model", output_guard="terminal_answer_removed")
    return {"response": response}


@lru_cache(maxsize=1)
def _graph():
    graph = StateGraph(Turn)
    graph.add_node("evidence", _evidence)
    graph.add_node("policy", _policy)
    graph.add_node("model", _generate)
    graph.add_edge(START, "evidence")
    graph.add_edge("evidence", "policy")
    graph.add_conditional_edges("policy", lambda turn: "model" if turn["needs_model"] else "done",
                               {"model": "model", "done": END})
    graph.add_edge("model", END)
    return graph.compile()


def run_turn(problem: dict, state: dict, events: list, request: dict, *, model_client=None) -> dict:
    """호출 실패 시 상태를 저장하지 않도록 복사본에서 한 교육 행동만 실행한다."""
    if request.get("intent") not in {"hint", "submit", "more_help", "answer"}:
        raise ValueError("지원하지 않는 튜터 요청입니다.")
    if not isinstance(problem.get("target_text"), str) or not problem["target_text"].strip():
        raise ValueError("튜터와 풀 문제를 먼저 확인해 주세요.")
    current = initial_state()
    current.update(deepcopy(state))
    result = _graph().invoke({"problem": deepcopy(problem), "state": current,
                             "events": deepcopy(events), "request": deepcopy(request), "model_client": model_client})
    return {"response": result["response"], "state": result["state"]}
