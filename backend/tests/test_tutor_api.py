"""실제 쿠키와 임시 DB로 튜터의 소유권·중복·응답 공개 경계를 검사한다."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, nullcontext
from copy import deepcopy
from io import BytesIO
import importlib.util
import hashlib
import json
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


def test_work_photo_keeps_ocr_draft_separate_from_student_submission(tutor, monkeypatch):
    _, app, client, runner, _ = tutor
    session = start(client, problem(client))
    endpoint = f"/tutor-sessions/{session['id']}"
    image = BytesIO()
    Image.new("RGB", (8, 8), "white").save(image, format="PNG")
    upload = {"file": ("work.png", image.getvalue(), "image/png")}

    class PhotoReader:
        finish_reason = "stop"

        def connect(self):
            return nullcontext(self)

        def stream(self, image, prompt, _max_tokens):
            assert image.startswith(b"\x89PNG") and "Preserve wrong calculations" in prompt
            yield {"type": "complete", "finish_reason": self.finish_reason, "text": json.dumps({"lines": [
                {"text": "x=5", "state": "active"},
                {"text": "x=4", "state": "deleted"},
                {"text": "x=?", "state": "active"},
            ]})}

    reader = PhotoReader()
    monkeypatch.setattr(submission_api, "build_qwen_backend", lambda: reader)
    with closing(TestClient(app)) as other:
        assert other.post(endpoint + "/work-photo", files=upload).status_code == 401
        login(other, "photo-other@example.com")
        assert other.post(endpoint + "/work-photo", files=upload).status_code == 404
    assert client.post(endpoint + "/work-photo", files={"file": ("bad.png", b"not a picture")}).status_code == 400

    recognized = client.post(endpoint + "/work-photo", files=upload)
    assert recognized.status_code == 200, recognized.text
    assert recognized.json()["lines"] == [
        {"text": "x=5", "state": "active"},
        {"text": "x=4", "state": "deleted"},
        {"text": "x=?", "state": "uncertain"},
    ]
    assert recognized.json()["recognition_seconds"] >= 0
    reader.finish_reason = "length"
    assert client.post(endpoint + "/work-photo", files=upload).status_code == 503
    assert client.get(endpoint).json()["events"] == []
    assert len(client.get("/submissions").json()) == 1
    assert runner.calls == []

    reviewed = [{"id": "confirmed-line", "text": "x=7", "state": "active"}]
    response = client.post(endpoint + "/turns", json={**turn(intent="submit"), "steps": reviewed})
    assert response.status_code == 200, response.text
    assert client.get(endpoint).json()["events"][-1]["steps"] == reviewed


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
    targets_url = f"/submissions/{ident}/tutor-targets"
    assert client.get(targets_url).json()["saved_sessions"] == []
    session = start(client, ident)
    assert session["submission_id"] == ident and session["input_kind"] == "photo"
    assert client.get(targets_url).json()["saved_sessions"] == [
        {"id": session["id"], "target_text": session["target_text"]},
    ]
    assert len(client.get("/submissions").json()) == 1
    updated = client.post(f"/submissions/{ident}/confirm", json={
        "content_text": "5x=10", "expected_version_number": 1,
    })
    assert updated.status_code == 200, updated.text
    targets = client.get(targets_url).json()
    assert targets["input_version_number"] == 2 and targets["saved_sessions"] == []
    assert client.get(f"/tutor-sessions/{session['id']}").json()["is_current"] is False
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


MULTI_QUESTION = r'''다음 방정식을 풀어라.
(1) \(\frac{4}{3}x-3=x+6\)
(2) \(\frac{1}{4}x-\frac{1}{2}=-\frac{2}{5}x+2\)
(3) \(\frac{2}{3}x-\frac{1}{6}=\frac{3}{2}x\)'''


def confirmed_multi_problem(client):
    response = client.post('/submissions/text', json={'content_text': MULTI_QUESTION})
    assert response.status_code == 201
    ident = response.json()['id']
    assert client.post(f'/submissions/{ident}/confirm', json={
        'content_text': MULTI_QUESTION, 'expected_version_number': 1}).status_code == 200
    return ident


def test_target_choices_preserve_source_and_keep_cookie_ownership(tutor):
    _, app, client, runner, _ = tutor
    ident = confirmed_multi_problem(client)
    endpoint = f'/submissions/{ident}/tutor-targets'
    choices = client.get(endpoint)
    assert choices.status_code == 200, choices.text
    value = choices.json()
    assert value['submission_id'] == ident and value['input_version_number'] == 1
    assert value['status'] == 'selection_required' and len(value['choices']) == 3
    assert value['choices'][0]['target_text'] == '(4/3)*x-3=x+6'
    assert client.post(f'/submissions/{ident}/tutor-sessions', json={
        'expected_input_version_number': 1, 'target_text': MULTI_QUESTION}).status_code == 422
    selected = client.post(f'/submissions/{ident}/tutor-sessions', json={
        'expected_input_version_number': 1, 'target_text': value['choices'][0]['target_text']})
    assert selected.status_code == 201, selected.text
    assert selected.json()['source_text'] == MULTI_QUESTION
    assert selected.json()['target_selection']['status'] == 'single_equation'
    with TestClient(app) as other:
        assert other.get(endpoint).status_code == 401
        login(other, 'target-other@example.com')
        assert other.get(endpoint).status_code == 404
    assert runner.calls == []


def test_legacy_multi_target_can_recover_without_changing_its_history(tutor):
    database, _, client, runner, owner = tutor
    ident = confirmed_multi_problem(client)
    # 수정 전 저장 형식의 세션과 공개 이력을 임시 DB에 그대로 재현한다.
    from app.tutor.session_service import initial_state
    with submission_connection(database, write=True) as connection:
        version_id = connection.execute('SELECT id FROM problem_input_versions WHERE submission_id=?', (ident,)).fetchone()[0]
        problem_json = {'source_text': MULTI_QUESTION, 'target_text': MULTI_QUESTION,
                        'input_version_number': 1, 'input_kind': 'text', 'subject': 'mathematics'}
        cursor = connection.execute('''INSERT INTO tutor_sessions(submission_id,input_version_id,
            subject,target_text,target_sha256,problem_json,state_json) VALUES(?,?,?,?,?,?,?)''',
            (ident, version_id, 'mathematics', MULTI_QUESTION, hashlib.sha256(MULTI_QUESTION.encode()).hexdigest(),
             json.dumps(problem_json), json.dumps(initial_state())))
        old_id = cursor.lastrowid
        request = turn('legacy-multi-001')
        response = {'action': 'guide', 'message': '양변에 3을 곱해 보세요.', 'verification': None}
        connection.execute('''INSERT INTO tutor_turns(session_id,request_id,request_sha256,
            expected_revision,request_json,status,lease_token,lease_expires_at,response_json,state_after_json)
            VALUES(?,?,?,?,?,'completed',?,0,?,?)''', (old_id, request['request_id'], 'legacy', 0,
            json.dumps(request), 'legacy', json.dumps(response), json.dumps(initial_state())))
    legacy = client.get(f'/tutor-sessions/{old_id}').json()
    assert legacy['is_current'] is True and legacy['can_submit'] is False
    assert legacy['target_selection']['status'] == 'selection_required'
    assert len(legacy['events']) == 1
    assert client.post(f'/tutor-sessions/{old_id}/turns', json=turn()).status_code == 409
    recovered = client.post(f'/submissions/{ident}/tutor-sessions', json={
        'expected_input_version_number': 1, 'target_text': '(4/3)*x-3=x+6'})
    assert recovered.status_code == 201, recovered.text
    assert recovered.json()['id'] != old_id and recovered.json()['events'] == []
    assert client.get(f'/tutor-sessions/{old_id}').json() == legacy
    assert runner.calls == []


def test_selected_fraction_work_is_verified_without_calling_the_model(tutor):
    database, _, client, _, _ = tutor
    from app.tutor.session_service import run_turn

    class NoModel:
        def generate(self, *_args):
            raise AssertionError('완성한 풀이 검산에서 모델을 호출하면 안 됩니다.')

    def exact_runner(problem, state, events, request):
        return run_turn(problem, state, events, request, model_client=NoModel())

    ident = confirmed_multi_problem(client)
    with TestClient(create_student_app(database, pipeline_factory=no_pipeline, tutor_runner=exact_runner)) as checked:
        login(checked, register=False)
        targets = checked.get(f'/submissions/{ident}/tutor-targets').json()['choices']
        session = checked.post(f'/submissions/{ident}/tutor-sessions', json={
            'expected_input_version_number': 1, 'target_text': targets[0]['display_text']}).json()
        body = {**turn('fraction-complete-001', intent='submit'), 'student_text': '', 'steps': [
            {'id': 's1', 'text': '4x-9=3x+18', 'state': 'active'},
            {'id': 's2', 'text': 'x=27', 'state': 'active'}]}
        result = checked.post(f"/tutor-sessions/{session['id']}/turns", json=body)
        assert result.status_code == 200, result.text
        assert result.json()['state']['solved'] is True
        assert result.json()['events'][-1]['response']['model'] is None
        assert result.json()['events'][-1]['response']['verification']['judgement'] == 'correct'
