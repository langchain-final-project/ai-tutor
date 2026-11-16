# 확인된 문제·풀이를 새로 검증하고 안내와 모델 호출 계획을 반환함
"""
사용: evaluate_reviewed_solution(problem, work, context_mode="compact")
지원: 실수에서 유일해를 갖는 일원일차방정식. 사진 인식·모델 실행은 상위 서비스가 담당함.

입력은 아래 필드를 사용
- problem: schema_version="verification-problem-v1", id, revision, equation,
  variable(영문 한 글자), domain="real", confirmed(bool), profile="linear-equation-v1"
- work: schema_version="verification-work-v1", problem_id, problem_revision, steps
- steps의 각 줄: id, text, state(active/deleted/uncertain), confirmed(bool)
  최대100줄이며 풀이가 없으면 []로 첫 힌트 흐름을 시작

반환값(solution-evaluation-v1):
- verification: 줄별 판정·최초 오류·완료 여부. 앞선 오류는 마지막 정답으로 지워지지 않음
- error_evidence / student_feedback: 재확인 근거와 고정 안내.
- model_route / model_context: 후속 모델 작업 계획과 판정 문맥. 호출 불필요 시 문맥은 None.
- input_fingerprint: 입력 변경 확인용 지문. 승인·인증을 대신하지 않는다.

사용자 확인과 버전 관리는 상위 서버가 맡고, 입력 수정 후에는 다시 호출함
이전 판정이나 모델 응답은 입력받지 않음. 모델 문맥에는 확인된 문제·풀이를
별도로 연결하되 평가용 정답은 넣지 않음. compact는 요약, full은 상세 근거임
실제 모델 호출과 생성 힌트 공개는 이 함수가 수행하지 않음

계약 오류는 ValueError, 미확인·미지원·버전 불일치는 결과 상태로 반환
실행 환경은 Python 3.12 이상. backend/tests에서 코어 테스트를 실행:
python3 -B -m unittest -q test_step_verifier test_input_binding test_service_feedback test_solution_service
"""

from copy import deepcopy

from . import guided_feedback, step_verifier


def evaluate_reviewed_solution(
    problem: dict, work: dict, *, context_mode: str = "compact"
) -> dict:
    """입력을 복사해 새 판정·오류 근거·고정 안내·호출 계획을 함께 계산한다."""
    if context_mode not in ("compact", "full"):
        raise ValueError("context_mode는 compact 또는 full이어야 합니다.")

    problem_snapshot, work_snapshot = deepcopy((problem, work))
    result = step_verifier.verify(problem_snapshot, work_snapshot)
    evidence = guided_feedback.error_evidence(problem_snapshot, work_snapshot, result)
    feedback = guided_feedback.student_feedback(problem_snapshot, work_snapshot, result)
    model_route = guided_feedback.route(result)
    model_context = (
        guided_feedback.context(problem_snapshot, work_snapshot, result, context_mode)
        if model_route["model_required"] else None
    )
    return {
        "schema_version": "solution-evaluation-v1",
        "input_fingerprint": result["input_fingerprint"],
        "verification": result,
        "error_evidence": evidence,
        "student_feedback": feedback,
        "model_route": model_route,
        "model_context": model_context,
    }
