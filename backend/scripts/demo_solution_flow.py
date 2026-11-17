# 실행 중인 로컬 API에 새 개발 질문을 만들고 저장·확인·검증·복원을 확인

# 실행: python backend/scripts/demo_solution_flow.py --token 개발용토큰
# 모델은 호출하지 않지만 새 질문과 검증·표시 확인 기록을 실제로 저장

import argparse
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


MAX_RESPONSE_BYTES = 1024 * 1024


def http_transport(base_url, token):
    parsed = urlsplit(base_url)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"}):
        raise ValueError("개발 예제의 base-url은 로컬 HTTP 서버여야 합니다.")

    def request(method, path, payload=None):
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = Request(base_url.rstrip("/") + path, data=data, method=method, headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        })
        try:
            with urlopen(req, timeout=20) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise ValueError("API 응답이 너무 큽니다.")
                return response.status, json.loads(raw)
        except HTTPError as error:
            raise ValueError(f"API 요청 실패: {method} {path}, HTTP {error.code}") from None
    return request


def run_flow(request, *, output=print):
    def call(label, method, path, payload=None):
        status, data = request(method, path, payload)
        if status not in {200, 201}:
            raise ValueError(f"{label}: 예상하지 못한 HTTP {status}")
        output(f"[{label}] HTTP {status}")
        return data

    question = call("1/7 질문 등록", "POST", "/submissions/text", {"content_text": "2x+3=11"})
    submission_id = question["id"]
    path = f"/submissions/{submission_id}"
    problem_version = question["latest_input"]["version_number"]
    call("2/7 문제 확인", "POST", path + "/confirm", {
        "content_text": "2x+3=11", "expected_version_number": problem_version,
    })
    work = call("3/7 풀이 저장", "PUT", path + "/work", {
        "expected_problem_version_number": problem_version,
        "expected_work_version_number": 0,
        "steps": [
            {"id": "s1", "text": "2x=8", "state": "active"},
            {"id": "s2", "text": "x=4", "state": "active"},
        ],
    })
    versions = {
        "expected_problem_version_number": problem_version,
        "expected_work_version_number": work["version_number"],
    }
    call("4/7 풀이 확인", "POST", path + "/work/confirm", versions)
    result = call("5/7 검증", "POST", path + "/verify", versions)
    restored = call("6/7 저장 결과 조회", "GET", path + "/verification")
    if restored is None or restored["id"] != result["id"] or not restored["is_current"]:
        raise ValueError("저장한 현재 검증 결과가 복원되지 않았습니다.")
    evaluation = restored["evaluation"]
    if (evaluation["verification"]["judgement"] != "correct"
            or evaluation["verification"]["completion"] != "complete"
            or evaluation["model_route"]["model_required"]):
        raise ValueError("합성 정상 풀이의 검증 결과가 기대와 다릅니다.")
    call("7/7 안내 표시 확인 기록", "POST", path + f"/verification/{result['id']}/feedback-viewed")
    output(f"질문 ID: {submission_id}, 문제 버전: {problem_version}, 풀이 버전: {work['version_number']}")
    output("판정: correct / 완료: complete / 모델 호출 계획: 없음")
    output("안내: " + evaluation["student_feedback"]["hint"])
    return restored


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--token", required=True, help="서버와 같은 개발용 토큰")
    args = parser.parse_args()
    try:
        run_flow(http_transport(args.base_url, args.token))
    except (ValueError, KeyError, TypeError, URLError, OSError) as error:
        parser.exit(1, f"통합 예제 실패: {error}\n")


if __name__ == "__main__":
    main()
