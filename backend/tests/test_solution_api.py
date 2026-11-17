# 별도 SQLite와 OCR로 질문·풀이·검증 API의 연결을 확인
# OCR 은 아직 기능 구현 안되어서 그냥 임의

from contextlib import closing
from pathlib import Path
import sqlite3

from fastapi.testclient import TestClient
import pytest

from app.submission_api import create_submission_app


HEADERS = {"Authorization": "Bearer test-token"}
MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "solution-api.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        for name in (
            "001_problem_bank.sqlite.sql",
            "002_problem_classification.sqlite.sql",
            "004_problem_submissions.sqlite.sql",
            "005_problem_ocr_jobs.sqlite.sql",
            "006_solution_verification.sqlite.sql",
        ):
            connection.executescript((MIGRATIONS / name).read_text(encoding="utf-8"))
    return path


def no_pipeline():
    raise AssertionError("텍스트 검증에서 OCR·모델을 호출하면 안 됩니다.")


def make_app(database, owner="student_A"):
    return create_submission_app(
        database, dev_token="test-token", owner_id=owner, pipeline_factory=no_pipeline
    )


@pytest.fixture
def client(database):
    with TestClient(make_app(database)) as current:
        yield current


def create_problem(client, equation="2*(x+3)=10", confirmed=True):
    response = client.post("/submissions/text", headers=HEADERS, json={"content_text": equation})
    assert response.status_code == 201, response.text
    submission = response.json()
    assert submission["review_status"] == "needs_review"
    assert submission["latest_input"]["confirmed_at"] is None
    ident = submission["id"]
    if confirmed:
        response = client.post(
            f"/submissions/{ident}/confirm", headers=HEADERS,
            json={"content_text": equation, "expected_version_number": 1},
        )
        assert response.status_code == 200, response.text
    return ident


def versions(problem=1, work=0):
    return {"expected_problem_version_number": problem, "expected_work_version_number": work}


def save_work(client, ident, lines, *, problem=1, work=0, states=None, confirm=True):
    payload = versions(problem, work)
    payload["steps"] = [
        {"id": f"s{i+1}", "text": text, "state": states[i] if states else "active"}
        for i, text in enumerate(lines)
    ]
    response = client.put(f"/submissions/{ident}/work", headers=HEADERS, json=payload)
    assert response.status_code == 201, response.text
    value = response.json()
    assert value["confirmed_at"] is None
    if confirm:
        response = client.post(
            f"/submissions/{ident}/work/confirm", headers=HEADERS,
            json=versions(problem, value["version_number"]),
        )
        assert response.status_code == 200, response.text
        value = response.json()
        assert value["confirmed_at"] is not None
    return value


def evaluate(client, ident, problem=1, work=0):
    response = client.post(f"/submissions/{ident}/verify", headers=HEADERS, json=versions(problem, work))
    assert response.status_code == 201, response.text
    return response.json()


def test_text_question_first_hint_without_ocr(client):
    ident = create_problem(client)
    assert client.get(f"/submissions/{ident}/work", headers=HEADERS).json() is None
    result = evaluate(client, ident)
    assert result["work_version_number"] == 0
    assert result["is_current"] is True
    assert result["evaluation"]["verification"]["completion"] == "no_work"
    assert result["evaluation"]["model_route"]["reason"] == "first_hint_requested"
    assert result["evaluation"]["student_feedback"]["hint_template_id"] == "first_hint"


def test_unconfirmed_problem_requests_confirmation(client):
    ident = create_problem(client, confirmed=False)
    result = evaluate(client, ident)
    assert result["evaluation"]["verification"]["action"] == "ask_confirmation"
    assert result["evaluation"]["model_route"]["model_required"] is False


def test_draft_work_cannot_be_treated_as_confirmed(client):
    ident = create_problem(client)
    save_work(client, ident, ["x=2"], confirm=False)
    result = evaluate(client, ident, work=1)
    assert result["evaluation"]["verification"]["action"] == "ask_confirmation"
    assert result["evaluation"]["verification"]["judgement"] == "undetermined"
    client.post(f"/submissions/{ident}/work/confirm", headers=HEADERS, json=versions(work=1)).raise_for_status()
    old = client.get(f"/submissions/{ident}/verification/{result['id']}", headers=HEADERS).json()
    assert old["is_current"] is False
    assert client.get(f"/submissions/{ident}/verification", headers=HEADERS).json() is None
    assert evaluate(client, ident, work=1)["evaluation"]["verification"]["judgement"] == "correct"


@pytest.mark.parametrize("lines,judgement,completion,route", [
    (["2*x+6=10", "x=2"], "correct", "complete", "completed_correct"),
    (["x+3=5"], "correct", "incomplete", "unfinished_work"),
    (["2*x+3=10", "x=2"], "incorrect", "complete", "observed_error"),
])
def test_verified_work_routes_by_history(client, lines, judgement, completion, route):
    ident = create_problem(client)
    save_work(client, ident, lines)
    result = evaluate(client, ident, work=1)
    evaluated = result["evaluation"]
    assert evaluated["verification"]["judgement"] == judgement
    assert evaluated["verification"]["completion"] == completion
    assert evaluated["model_route"]["reason"] == route
    if judgement == "incorrect":
        assert evaluated["verification"]["first_error_step_id"] == "s1"
        assert evaluated["error_evidence"]["target_side"] == "lhs"


def test_uncertain_prefix_preserves_observed_error_without_claiming_first(client):
    ident = create_problem(client)
    save_work(client, ident, ["?", "x=4"], states=["uncertain", "active"])
    value = evaluate(client, ident, work=1)["evaluation"]["verification"]
    assert value["first_observed_error_step_id"] == "s2"
    assert value["first_error_step_id"] is None
    assert value["action"] == "ask_confirmation"


def test_confirmed_deleted_line_is_excluded(client):
    ident = create_problem(client)
    save_work(client, ident, ["x=4", "x=2"], states=["deleted", "active"])
    value = evaluate(client, ident, work=1)["evaluation"]["verification"]
    assert value["judgement"] == "correct"
    assert value["steps"][0]["skipped"] is True


def test_unsupported_equation_is_not_student_error(client):
    ident = create_problem(client, "x*x=4")
    save_work(client, ident, ["x=2"])
    value = evaluate(client, ident, work=1)["evaluation"]["verification"]
    assert value["judgement"] == "undetermined"
    assert value["action"] == "unsupported"


def test_old_versions_conflict_and_new_work_starts_for_new_problem(client):
    ident = create_problem(client)
    save_work(client, ident, ["x=2"])
    old = evaluate(client, ident, work=1)
    response = client.patch(
        f"/submissions/{ident}/input", headers=HEADERS,
        json={"content_text": "x=3", "expected_version_number": 1},
    )
    assert response.status_code == 200, response.text
    assert response.json()["latest_input"]["version_number"] == 2
    assert client.get(f"/submissions/{ident}/work", headers=HEADERS).json() is None
    assert client.post(f"/submissions/{ident}/verify", headers=HEADERS, json=versions(work=1)).status_code == 409
    assert client.post(f"/submissions/{ident}/work/confirm", headers=HEADERS, json=versions(work=1)).status_code == 409
    assert client.get(f"/submissions/{ident}/verification", headers=HEADERS).json() is None
    historical = client.get(f"/submissions/{ident}/verification/{old['id']}", headers=HEADERS).json()
    assert historical["is_current"] is False
    assert historical["evaluation"] == old["evaluation"]
    assert client.post(
        f"/submissions/{ident}/verification/{old['id']}/feedback-viewed", headers=HEADERS
    ).status_code == 409
    save_work(client, ident, ["x=3"], problem=2, work=0, confirm=False)
    assert evaluate(client, ident, problem=2, work=1)["evaluation"]["verification"]["action"] == "ask_confirmation"


def test_stale_work_write_is_rejected(client):
    ident = create_problem(client)
    save_work(client, ident, ["x=2"])
    response = client.put(
        f"/submissions/{ident}/work", headers=HEADERS,
        json={**versions(work=0), "steps": []},
    )
    assert response.status_code == 409
    current = client.get(f"/submissions/{ident}/work", headers=HEADERS).json()
    assert current["version_number"] == 1
    assert current["steps"][0]["text"] == "x=2"


def test_repeated_confirmation_is_idempotent(client):
    ident = create_problem(client)
    work = save_work(client, ident, ["x=2"])
    response = client.post(f"/submissions/{ident}/work/confirm", headers=HEADERS, json=versions(work=1))
    assert response.status_code == 200
    assert response.json()["confirmed_at"] == work["confirmed_at"]


def test_result_and_feedback_restore_after_restart(database):
    with TestClient(make_app(database)) as first:
        ident = create_problem(first)
        save_work(first, ident, ["2*x+3=10"])
        result = evaluate(first, ident, work=1)
        url = f"/submissions/{ident}/verification/{result['id']}"
        first.get(url, headers=HEADERS).raise_for_status()
        assert first.get(f"/submissions/{ident}/feedback/history", headers=HEADERS).json() == []
        one = first.post(url + "/feedback-viewed", headers=HEADERS)
        two = first.post(url + "/feedback-viewed", headers=HEADERS, json={})
        assert one.status_code == two.status_code == 200
        assert one.json() == two.json()
        assert one.json()["feedback"] == result["evaluation"]["student_feedback"]
    with TestClient(make_app(database)) as second:
        restored = second.get(f"/submissions/{ident}/verification", headers=HEADERS)
        assert restored.status_code == 200
        assert restored.json() == result
        assert len(second.get(f"/submissions/{ident}/verification/history", headers=HEADERS).json()) == 1
        assert second.get(f"/submissions/{ident}/feedback/history", headers=HEADERS).json() == [one.json()]


def test_auth_and_other_owner_cannot_read_or_write(database):
    with TestClient(make_app(database)) as owner:
        ident = create_problem(owner)
        save_work(owner, ident, ["x=2"])
        result = evaluate(owner, ident, work=1)
        assert owner.get(f"/submissions/{ident}/work").status_code == 401
        assert owner.post("/submissions/text", json={"content_text": "x=2"}).status_code == 401
    with TestClient(make_app(database, owner="student_B")) as other:
        base = f"/submissions/{ident}"
        for suffix in ["/work", "/verification", "/verification/history", f"/verification/{result['id']}", "/feedback/history"]:
            assert other.get(base + suffix, headers=HEADERS).status_code == 404
        assert other.put(base + "/work", headers=HEADERS, json={**versions(), "steps": []}).status_code == 404
        assert other.post(base + "/work/confirm", headers=HEADERS, json=versions(work=1)).status_code == 404
        assert other.post(base + "/verify", headers=HEADERS, json=versions(work=1)).status_code == 404
        assert other.post(base + f"/verification/{result['id']}/feedback-viewed", headers=HEADERS).status_code == 404


@pytest.mark.parametrize("payload", [
    {**versions(), "steps": [], "owner_id": "student_B"},
    {**versions(), "steps": [], "confirmed": True},
    {**versions(), "steps": [{"id": "s1", "text": "x=2", "state": "active", "confirmed": True}]},
    {**versions(), "steps": [{"id": " ", "text": "x=2", "state": "active"}]},
    {**versions(), "steps": [{"id": "s1", "text": "x=2", "state": "erased"}]},
    {**versions(), "steps": [{"id": f"s{i}", "text": "x=2", "state": "active"} for i in range(101)]},
    {**versions(), "expected_problem_version_number": True, "steps": []},
    {**versions(), "steps": [{"id": "s1", "text": "x=2", "state": "active"}] * 2},
])
def test_work_contract_rejects_untrusted_or_invalid_payload(client, payload):
    ident = create_problem(client)
    response = client.put(f"/submissions/{ident}/work", headers=HEADERS, json=payload)
    assert response.status_code == 422, response.text
    assert client.get(f"/submissions/{ident}/work", headers=HEADERS).json() is None


def test_verdict_and_hint_cannot_be_supplied_by_client(client):
    ident = create_problem(client)
    assert client.post(
        f"/submissions/{ident}/verify", headers=HEADERS,
        json={**versions(), "evaluation": {"judgement": "correct"}},
    ).status_code == 422
    result = evaluate(client, ident)
    assert client.post(
        f"/submissions/{ident}/verification/{result['id']}/feedback-viewed", headers=HEADERS,
        json={"hint": "정답은 2입니다."},
    ).status_code == 422


def test_result_must_belong_to_requested_submission(client):
    first = create_problem(client)
    second = create_problem(client)
    result = evaluate(client, first)
    response = client.get(f"/submissions/{second}/verification/{result['id']}", headers=HEADERS)
    assert response.status_code == 404


def test_positive_path_ids_required(client):
    assert client.get("/submissions/0/work", headers=HEADERS).status_code == 422


def test_submission_list_restores_only_current_owner_questions(database):
    with TestClient(make_app(database)) as owner:
        first = create_problem(owner)
        second = create_problem(owner, "x=3")
        response = owner.get("/submissions?limit=1", headers=HEADERS)
        assert response.status_code == 200
        assert [row["id"] for row in response.json()] == [second]
        assert owner.get("/submissions?limit=0", headers=HEADERS).status_code == 422
        assert owner.get("/submissions?limit=101", headers=HEADERS).status_code == 422
        assert owner.get("/submissions").status_code == 401
    with TestClient(make_app(database, owner="student_B")) as other:
        assert other.get("/submissions", headers=HEADERS).json() == []
        third = create_problem(other)
        assert [row["id"] for row in other.get("/submissions", headers=HEADERS).json()] == [third]
    with TestClient(make_app(database)) as restored:
        assert [row["id"] for row in restored.get("/submissions", headers=HEADERS).json()] == [second, first]
