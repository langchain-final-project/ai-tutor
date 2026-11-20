"""실제 쿠키와 임시 DB로 튜터의 소유권·중복·응답 공개 경계를 검사한다."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from io import BytesIO
import importlib.util
from pathlib import Path
import sqlite3
from threading import Event

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from app import submission_api
from app.db.auth import metadata
from app.db.connection import build_engine
from app.db.submissions import save_student_input_version, submission_connection
from app.student_api import create_student_app
from app.tutor.unit_type_matcher import UnitTypeMatcher


PASSWORD = "tutor-api-test-password"
QUESTION = "2(x+2)=3(x-1)"
SCRIPT = Path(__file__).resolve().parents[1] / "scripts/setup_solution_flow.py"
spec = importlib.util.spec_from_file_location("tutor_api_solution_setup", SCRIPT)
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


def no_pipeline():
    raise AssertionError("튜터 API 검사에서 실제 OCR 모델을 실행하면 안 됩니다.")


class Runner:
    """교육 정책 대신 저장 경계를 확인할 수 있는 작은 실행 대역이다."""

    def __init__(self):
        self.calls = []
        self.hook = None

    def __call__(self, problem, state, events, request):
        self.calls.append(deepcopy(request))
        if self.hook:
            self.hook(problem, state, events, request)
        updated = deepcopy(state)
        updated["attempts"] += int(request["intent"] == "submit")
        updated["last_student_text"] = request["student_text"]
        return {
            "state": updated,
            "response": {
                "action": "hint", "reason": "api_test", "policy_version": "test-policy",
                "message": "다음 한 단계를 확인해 보세요.", "verification": None,
                "model": "test-runner", "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        }


def login(client, email="tutor-first@example.com", *, register=True):
    if register:
        response = client.post("/auth/signup", json={
            "name": "튜터 검사", "email": email, "password": PASSWORD,
            "password_confirmation": PASSWORD,
        })
        assert response.status_code == 201, response.text
    response = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()["user_id"]


@pytest.fixture
def tutor(tmp_path, monkeypatch):
    database = tmp_path / "tutor.sqlite3"
    setup.prepare(database, create_empty=True)
    engine = build_engine(f"sqlite:///{database}")
    try:
        metadata.create_all(engine)
    finally:
        engine.dispose()
    matcher = UnitTypeMatcher([{
        "id": "tutor-api-fixture", "question_text": QUESTION, "figure_text": None,
        "source_topic_code": "7103048", "source_topic_name": "괄호가 있는 일차방정식의 풀이",
    }])
    monkeypatch.setattr(submission_api, "get_unit_matcher", lambda _path: matcher)
    runner = Runner()
    app = create_student_app(database, pipeline_factory=no_pipeline, tutor_runner=runner)
    with TestClient(app) as client:
        owner = login(client)
        yield database, app, client, runner, owner


def problem(client, *, confirm=True):
    response = client.post("/submissions/text", json={"content_text": QUESTION})
    assert response.status_code == 201, response.text
    submission = response.json()
    if confirm:
        response = client.post(f"/submissions/{submission['id']}/confirm", json={
            "content_text": QUESTION, "expected_version_number": 1,
        })
        assert response.status_code == 200, response.text
    return submission["id"]


def start(client, submission_id):
    response = client.post(f"/submissions/{submission_id}/tutor-sessions", json={
        "expected_input_version_number": 1, "target_text": QUESTION,
        "subject": "mathematics",
    })
    assert response.status_code == 201, response.text
    return response.json()


def turn(request_id="tutor-request-001", *, revision=0, intent="hint"):
    return {"request_id": request_id, "expected_revision": revision,
            "intent": intent, "student_text": "다음 부분을 확인해 주세요." if intent == "submit" else "",
            "steps": []}


def test_tutor_requires_cookie_and_keeps_other_students_out(tutor):
    _, app, client, runner, _ = tutor
    ident = problem(client)
    session = start(client, ident)
    endpoint = f"/tutor-sessions/{session['id']}"
    with closing(TestClient(app)) as anonymous:
        for headers in ({}, {"Authorization": "Bearer development-token"}):
            assert anonymous.get("/tutor-sessions", headers=headers).status_code == 401
            assert anonymous.get(endpoint, headers=headers).status_code == 401
            assert anonymous.post(endpoint + "/turns", json=turn(), headers=headers).status_code == 401
        login(anonymous, "tutor-second@example.com")
        assert anonymous.get("/tutor-sessions").json() == []
        assert anonymous.get(endpoint).status_code == 404
        assert anonymous.post(endpoint + "/turns", json=turn()).status_code == 404
        assert anonymous.post(f"/submissions/{ident}/tutor-sessions", json={
            "expected_input_version_number": 1, "target_text": QUESTION,
        }).status_code == 404
    assert runner.calls == []


def test_start_needs_confirmed_current_input_and_is_idempotent(tutor):
    _, _, client, runner, _ = tutor
    ident = problem(client, confirm=False)
    endpoint = f"/submissions/{ident}/tutor-sessions"
    body = {"expected_input_version_number": 1, "target_text": QUESTION}
    assert client.post(endpoint, json=body).status_code == 409
    assert client.post(f"/submissions/{ident}/confirm", json={
        "content_text": QUESTION, "expected_version_number": 1,
    }).status_code == 200
    first = start(client, ident)
    assert start(client, ident)["id"] == first["id"]
    assert first["source_text"] == QUESTION
    assert first["type_match"]["selected_type"]["source_topic_code"] == "7103048"
    assert first["events"] == [] and first["revision"] == 0
    assert client.post(endpoint, json={**body, "expected_input_version_number": 2}).status_code == 409
    assert len(client.get("/tutor-sessions").json()) == 1
    assert runner.calls == []


class PhotoPipeline:
    def stream(self, image):
        assert image
        yield {"type": "result", "result": {
            "status": "needs_review", "text": QUESTION, "rotation_ccw": 0,
            "orientation": {"status": "corrected", "rotation_ccw": 0},
            "layout_detection": {"status": "unavailable", "layout": None, "report": None},
            "issues": [], "regions": [],
        }}


def test_confirmed_photo_can_start_without_creating_equation_submission(tutor):
    _, app, client, runner, _ = tutor
    image = BytesIO()
    Image.new("RGB", (8, 8), "white").save(image, format="PNG")
    response = client.post("/submissions/photo", files={
        "files": ("tutor-photo.png", image.getvalue(), "image/png"),
    })
    assert response.status_code == 201, response.text
    ident = response.json()["id"]
    app.state.ocr_jobs.pipeline_factory = PhotoPipeline
    assert client.post(f"/submissions/{ident}/ocr").status_code == 202
    app.state.ocr_jobs.executor.submit(lambda: None).result(timeout=10)
    confirmed = client.post(f"/submissions/{ident}/confirm", json={
        "content_text": QUESTION, "expected_version_number": 1,
    })
    assert confirmed.status_code == 200, confirmed.text
    session = start(client, ident)
    assert session["submission_id"] == ident and session["input_kind"] == "photo"
    assert len(client.get("/submissions").json()) == 1
    assert runner.calls == []


def test_turn_releases_database_lock_and_replay_does_not_call_model(tutor):
    database, _, client, runner, _ = tutor
    session = start(client, problem(client))
    endpoint = f"/tutor-sessions/{session['id']}"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE tutor_write_probe(value INTEGER)")
        connection.commit()

    def write_during_model(*_args):
        # 모델 호출 중 별도 연결에서 즉시 커밋할 수 있어야 한다.
        with closing(sqlite3.connect(database, timeout=0.1)) as connection:
            connection.execute("INSERT INTO tutor_write_probe VALUES (1)")
            connection.commit()

    runner.hook = write_during_model
    body = turn()
    response = client.post(endpoint + "/turns", json=body)
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["revision"] == 1 and len(saved["events"]) == 1
    assert client.post(endpoint + "/turns", json=body).json() == saved
    assert client.get(endpoint).json() == saved
    assert client.post(endpoint + "/turns", json={**body, "student_text": "다른 내용"}).status_code == 409
    assert client.post(endpoint + "/turns", json=turn("tutor-request-stale")).status_code == 409
    assert len(runner.calls) == 1
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tutor_write_probe").fetchone() == (1,)
    assert client.post("/auth/logout").status_code == 204
    login(client, register=False)
    assert client.get(endpoint).json() == saved
    assert len(runner.calls) == 1


def test_pending_duplicate_is_observable_and_does_not_run_twice(tutor):
    _, _, client, runner, _ = tutor
    session = start(client, problem(client))
    endpoint = f"/tutor-sessions/{session['id']}"
    entered, release = Event(), Event()

    def wait_for_release(*_args):
        entered.set()
        assert release.wait(timeout=10)

    runner.hook = wait_for_release
    body = turn()
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(client.post, endpoint + "/turns", json=body)
        try:
            assert entered.wait(timeout=10)
            duplicate = client.post(endpoint + "/turns", json=body)
            assert duplicate.status_code == 202, duplicate.text
            assert duplicate.json()["pending_request"]["request_id"] == body["request_id"]
            assert duplicate.json()["events"] == []
            assert client.post(endpoint + "/turns", json=turn("tutor-other-pending")).status_code == 409
            assert len(runner.calls) == 1
        finally:
            release.set()
        completed = first.result(timeout=10)
    assert completed.status_code == 200, completed.text
    assert len(completed.json()["events"]) == 1


def test_changed_source_during_model_does_not_publish_old_response(tutor):
    database, _, client, runner, owner = tutor
    ident = problem(client)
    session = start(client, ident)
    endpoint = f"/tutor-sessions/{session['id']}"

    def edit_during_model(*_args):
        with submission_connection(database, write=True) as connection:
            save_student_input_version(connection, submission_id=ident, owner_id=owner,
                                       content_text="x+1=8", expected_version_number=1)

    runner.hook = edit_during_model
    response = client.post(endpoint + "/turns", json=turn())
    assert response.status_code == 409, response.text
    saved = client.get(endpoint).json()
    assert saved["is_current"] is False
    assert saved["events"] == [] and saved["revision"] == 0
    assert saved["pending_request"]["status"] == "stale"
    assert client.post(endpoint + "/turns", json=turn()).status_code == 409
    assert len(runner.calls) == 1


def test_failed_request_preserves_input_and_same_key_retry_counts_once(tutor):
    _, _, client, runner, _ = tutor
    session = start(client, problem(client))
    endpoint = f"/tutor-sessions/{session['id']}"

    def fail_once(*_args):
        if len(runner.calls) == 1:
            raise TimeoutError("검사용 시간 초과")

    runner.hook = fail_once
    body = turn(intent="submit")
    assert client.post(endpoint + "/turns", json=body).status_code == 503
    failed = client.get(endpoint).json()
    assert failed["revision"] == 0 and failed["state"]["attempts"] == 0
    assert failed["events"] == []
    assert failed["pending_request"]["status"] == "failed"
    assert failed["pending_request"]["student_text"] == body["student_text"]
    retried = client.post(endpoint + "/turns", json=body)
    assert retried.status_code == 200, retried.text
    assert retried.json()["revision"] == 1 and retried.json()["state"]["attempts"] == 1
    assert len(retried.json()["events"]) == 1 and len(runner.calls) == 2
    assert client.post(endpoint + "/turns", json=body).json() == retried.json()
    assert len(runner.calls) == 2


def test_running_retry_takes_priority_over_later_failed_request(tutor):
    _, _, client, runner, _ = tutor
    session = start(client, problem(client))
    endpoint = f"/tutor-sessions/{session['id']}"
    first_body = turn("tutor-failed-first")
    later_body = turn("tutor-failed-later")

    def fail_request(*_args):
        raise TimeoutError("검사용 시간 초과")

    runner.hook = fail_request
    assert client.post(endpoint + "/turns", json=first_body).status_code == 503
    assert client.post(endpoint + "/turns", json=later_body).status_code == 503
    entered, release = Event(), Event()

    def wait_for_release(*_args):
        entered.set()
        assert release.wait(timeout=10)

    runner.hook = wait_for_release
    with ThreadPoolExecutor(max_workers=1) as executor:
        retry = executor.submit(client.post, endpoint + "/turns", json=first_body)
        try:
            assert entered.wait(timeout=10)
            # 생성 순서가 늦은 실패보다 실제 실행 중인 재시도를 보여야 한다.
            current = client.get(endpoint).json()
            assert current["pending_request"]["request_id"] == first_body["request_id"]
            assert current["pending_request"]["status"] == "running"
            duplicate = client.post(endpoint + "/turns", json=first_body)
            assert duplicate.status_code == 202, duplicate.text
            assert duplicate.json()["pending_request"]["request_id"] == first_body["request_id"]
            assert len(runner.calls) == 3
        finally:
            release.set()
        completed = retry.result(timeout=10)

    assert completed.status_code == 200, completed.text
    saved = client.get(endpoint).json()
    assert saved["revision"] == 1
    assert saved["pending_request"] is None
    assert [event["request_id"] for event in saved["events"]] == [first_body["request_id"]]
    assert len(runner.calls) == 3
