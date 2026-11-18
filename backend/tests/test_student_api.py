# 같은 SQLite의 실제 세션 쿠키로 학생별 문제·풀이 접근을 검사

from contextlib import closing
from io import BytesIO
import importlib.util
from pathlib import Path
import sqlite3
import time

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from app.auth import SESSION_COOKIE, token_digest
from app.db.auth import metadata
from app.db.connection import build_engine
from app.student_api import create_student_app


PASSWORD = "student-test-password"
SCRIPT = Path(__file__).resolve().parents[1] / "scripts/setup_solution_flow.py"
spec = importlib.util.spec_from_file_location("student_solution_setup", SCRIPT)
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


def no_pipeline():
    raise AssertionError("학생 계정 연결 검사에서 실제 OCR·모델을 호출하면 안 됩니다.")


@pytest.fixture
def database(tmp_path):
    database = tmp_path / "students.sqlite3"
    setup.prepare(database, create_empty=True)
    engine = build_engine(f"sqlite:///{database}")
    try:
        metadata.create_all(engine)
    finally:
        engine.dispose()
    return database


def make_app(database):
    return create_student_app(database, pipeline_factory=no_pipeline)


def register_and_login(client, email):
    signup = client.post("/auth/signup", json={
        "name": "테스트 학생", "email": email, "password": PASSWORD,
        "password_confirmation": PASSWORD,
    })
    assert signup.status_code == 201, signup.text
    user_id = signup.json()["user_id"]
    login = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    assert login.json()["user_id"] == user_id
    return user_id


@pytest.fixture
def students(database):
    app = make_app(database)
    with TestClient(app) as first:
        # 두 클라이언트의 쿠키는 분리하고 앱의 수명 주기는 한 번만 시작한다.
        second = TestClient(app)
        try:
            first_id = register_and_login(first, "first@example.com")
            second_id = register_and_login(second, "second@example.com")
            yield app, first, second, first_id, second_id
        finally:
            second.close()
    assert app.state.ocr_jobs.executor is None


def create_problem(client):
    response = client.post("/submissions/text", json={"content_text": "3(x-2)=12"})
    assert response.status_code == 201, response.text
    submission = response.json()
    confirmed = client.post(f"/submissions/{submission['id']}/confirm", json={
        "content_text": "3(x-2)=12", "expected_version_number": 1,
    })
    assert confirmed.status_code == 200, confirmed.text
    return submission


def verify_problem(client, submission_id):
    base = f"/submissions/{submission_id}"
    versions = {"expected_problem_version_number": 1, "expected_work_version_number": 1}
    saved = client.put(base + "/work", json={
        **versions, "expected_work_version_number": 0,
        "steps": [
            {"id": "s1", "text": "x-2=4", "state": "active"},
            {"id": "s2", "text": "x=6", "state": "active"},
        ],
    })
    assert saved.status_code == 201, saved.text
    assert client.post(base + "/work/confirm", json=versions).status_code == 200
    result = client.post(base + "/verify", json=versions)
    assert result.status_code == 201, result.text
    value = result.json()
    assert value["evaluation"]["verification"]["judgement"] == "correct"
    assert value["evaluation"]["verification"]["completion"] == "complete"
    viewed = client.post(base + f"/verification/{value['id']}/feedback-viewed", json={})
    assert viewed.status_code == 200, viewed.text
    return value, viewed.json()


def test_login_cookie_owns_submission_in_the_same_database(database, students):
    app, first, _, first_id, _ = students
    assert first.get("/health").json() == {"status": "ok", "database": "sqlite"}
    assert first.get("/me").json()["user_id"] == first_id
    submission = create_problem(first)
    assert submission["owner_id"] == first_id
    assert [row["id"] for row in first.get("/submissions").json()] == [submission["id"]]
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT owner_id FROM problem_submissions").fetchone() == (first_id,)
        assert connection.execute("SELECT COUNT(*) FROM users").fetchone() == (2,)
    assert app.state.ocr_jobs.executor is not None


def test_missing_cookie_cannot_fall_back_to_development_bearer(database, monkeypatch):
    monkeypatch.setenv("DIDGO_API_TOKEN", "development-token")
    monkeypatch.setenv("DIDGO_DEV_USER_ID", "local-student")
    with TestClient(make_app(database)) as client:
        for headers in ({}, {"Authorization": "Bearer development-token"}):
            assert client.get("/submissions", headers=headers).status_code == 401
            assert client.post("/submissions/text", headers=headers,
                               json={"content_text": "x=6"}).status_code == 401
        assert client.get("/me").status_code == 401


def test_client_cannot_select_another_owner(students):
    _, first, _, first_id, second_id = students
    response = first.post("/submissions/text", json={
        "content_text": "x=6", "owner_id": second_id,
    })
    assert response.status_code == 422
    assert first.get("/submissions").json() == []
    submission = create_problem(first)
    response = first.put(f"/submissions/{submission['id']}/work", json={
        "expected_problem_version_number": 1, "expected_work_version_number": 0,
        "steps": [], "owner_id": second_id,
    })
    assert response.status_code == 422
    assert first.get(f"/submissions/{submission['id']}").json()["owner_id"] == first_id


def test_other_account_cannot_read_or_change_text_solution(students):
    _, first, second, _, second_id = students
    submission = create_problem(first)
    result, _ = verify_problem(first, submission["id"])
    own = create_problem(second)
    assert own["owner_id"] == second_id
    assert [row["id"] for row in second.get("/submissions").json()] == [own["id"]]
    base = f"/submissions/{submission['id']}"
    for suffix in ("", "/work", "/input/history", "/ocr/history", "/verification",
                   "/verification/history", f"/verification/{result['id']}", "/feedback/history"):
        assert second.get(base + suffix).status_code == 404, suffix
    versions = {"expected_problem_version_number": 1, "expected_work_version_number": 1}
    assert second.put(base + "/work", json={**versions, "steps": []}).status_code == 404
    assert second.post(base + "/work/confirm", json=versions).status_code == 404
    assert second.post(base + "/verify", json=versions).status_code == 404
    assert second.post(base + f"/verification/{result['id']}/feedback-viewed", json={}).status_code == 404
    changed = {"content_text": "x=9", "expected_version_number": 1}
    assert second.patch(base + "/input", json=changed).status_code == 404
    assert second.post(base + "/confirm", json=changed).status_code == 404
    assert first.get(base + "/verification").json() == result


def test_other_account_cannot_access_photo_or_ocr_job(database, students):
    _, first, second, _, _ = students
    output = BytesIO()
    Image.new("RGB", (8, 8), "white").save(output, format="PNG")
    response = first.post("/submissions/photo", files={
        "files": ("equation.png", output.getvalue(), "image/png"),
    })
    assert response.status_code == 201, response.text
    ident = response.json()["id"]
    # 대기 작업을 기록해 조회 권한만 검사하고 OCR 스레드에는 전달하지 않는다.
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("INSERT INTO problem_ocr_jobs(id,submission_id) VALUES (?,?)",
                           ("owner-job", ident))
        connection.commit()
    assert first.get(f"/submissions/{ident}/photo").status_code == 200
    assert first.get("/ocr-jobs/owner-job").status_code == 200
    for path in (f"/submissions/{ident}", f"/submissions/{ident}/photo",
                 f"/submissions/{ident}/photo?selected=true&corrected=true",
                 f"/submissions/{ident}/ocr/history", "/ocr-jobs/owner-job",
                 "/ocr-jobs/owner-job/events"):
        assert second.get(path).status_code == 404, path
    assert second.post(f"/submissions/{ident}/ocr").status_code == 404


PHOTO_OCR_TEXT = r"다음 방정식을 풀어 보세요. \(3(x-2)=12\)"


class PhotoEquationPipeline:
    def stream(self, image):
        assert image
        yield {"type": "result", "result": {
            "status": "needs_review", "text": PHOTO_OCR_TEXT,
            "rotation_ccw": 90,
            "orientation": {"status": "corrected", "rotation_ccw": 90},
            "layout_detection": {"status": "unavailable", "layout": None, "report": None},
            "issues": [], "regions": [],
        }}


def photo_equation_source(app, client, *, recognize=True):
    output = BytesIO()
    Image.new("RGB", (8, 8), "white").save(output, format="PNG")
    response = client.post("/submissions/photo", files={
        "files": ("equation.png", output.getvalue(), "image/png"),
    })
    assert response.status_code == 201, response.text
    assert "photo_quality" in response.json()
    ident = response.json()["id"]
    if recognize:
        app.state.ocr_jobs.pipeline_factory = PhotoEquationPipeline
        job = client.post(f"/submissions/{ident}/ocr")
        assert job.status_code == 202, job.text
        app.state.ocr_jobs.executor.submit(lambda: None).result(timeout=10)
    return ident


def test_confirmed_photo_equation_keeps_raw_ocr_and_restores_source(students):
    app, first, second, owner_id, _ = students
    photo_id = photo_equation_source(app, first)
    photo = f"/submissions/{photo_id}"
    confirmed = first.post(photo + "/confirm", json={
        "content_text": PHOTO_OCR_TEXT, "expected_version_number": 1,
    })
    assert confirmed.status_code == 200, confirmed.text
    original = confirmed.json()
    assert original["latest_input"]["content_text"] == PHOTO_OCR_TEXT
    assert original["latest_input"]["version_number"] == 1
    response = first.post(photo + "/equation", json={
        "content_text": "3(x-2)=12", "expected_version_number": 1,
    })
    assert response.status_code == 201, response.text
    equation = response.json()
    assert equation["id"] != photo_id
    assert equation["input_kind"] == "text"
    assert equation["owner_id"] == owner_id
    assert equation["source_photo"] == {"submission_id": photo_id, "input_version_number": 1}
    assert equation["latest_input"]["content_text"] == "3(x-2)=12"
    assert equation["latest_input"]["confirmed_at"] is None
    assert first.get(photo).json() == original
    assert first.get(photo + "/ocr/history").json()[0]["ocr_text"] == PHOTO_OCR_TEXT
    base = f"/submissions/{equation['id']}"
    assert first.post(base + "/confirm", json={
        "content_text": "3(x-2)=12", "expected_version_number": 1,
    }).status_code == 200
    result, viewed = verify_problem(first, equation["id"])
    assert first.get(photo + "/work").status_code == 409
    for suffix in ("", "/work", "/verification", "/feedback/history"):
        assert second.get(base + suffix).status_code == 404, suffix
    assert second.post(photo + "/equation", json={
        "content_text": "x=6", "expected_version_number": 1,
    }).status_code == 404
    assert first.post("/auth/logout").status_code == 204
    assert first.get(base).status_code == 401
    assert first.post("/auth/login", json={"email": "first@example.com", "password": PASSWORD}).status_code == 200
    assert first.get(base).json()["source_photo"] == equation["source_photo"]
    assert first.get(base + "/verification").json() == result
    assert first.get(base + "/feedback/history").json() == [viewed]
    assert first.get(photo).json()["image_rotation_ccw"] == 90
    assert first.get(photo).json()["layout_detection"] == original["layout_detection"]

    revised = first.post(photo + "/confirm", json={
        "content_text": PHOTO_OCR_TEXT + " (학생이 확인한 원문 수정)", "expected_version_number": 1,
    })
    assert revised.status_code == 200, revised.text
    assert revised.json()["latest_input"]["version_number"] == 2
    assert first.get(base).json()["source_photo"] == equation["source_photo"]
    assert first.get(base + "/verification").json() == result
    assert first.post(photo + "/equation", json={
        "content_text": "x=6", "expected_version_number": 1,
    }).status_code == 409


def test_photo_equation_requires_confirmed_current_source_and_atomic_link(database, students):
    app, first, _, _, _ = students
    photo_id = photo_equation_source(app, first, recognize=False)
    photo = f"/submissions/{photo_id}"
    body = {"content_text": "3(x-2)=12", "expected_version_number": 1}
    assert first.post(photo + "/equation", json=body).status_code == 409
    app.state.ocr_jobs.pipeline_factory = PhotoEquationPipeline
    assert first.post(photo + "/ocr").status_code == 202
    app.state.ocr_jobs.executor.submit(lambda: None).result(timeout=10)
    assert first.post(photo + "/equation", json=body).status_code == 409
    assert first.post(photo + "/confirm", json={
        "content_text": PHOTO_OCR_TEXT, "expected_version_number": 1,
    }).status_code == 200
    for change in ({"content_text": " "}, {"content_text": "x" * 513},
                   {"owner_id": "forged-owner"}, {"expected_version_number": 0}):
        assert first.post(photo + "/equation", json={**body, **change}).status_code == 422
    assert first.post("/submissions/99999/equation", json=body).status_code == 404
    text = create_problem(first)
    assert first.post(f"/submissions/{text['id']}/equation", json=body).status_code == 409
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM photo_equation_sources").fetchone()[0] == 0
        before = connection.execute("SELECT COUNT(*) FROM problem_submissions").fetchone()[0]
        connection.executescript("""
            CREATE TRIGGER reject_equation_source_fixture BEFORE INSERT ON photo_equation_sources
            BEGIN SELECT RAISE(ABORT, 'fixture link failure'); END;
        """)
    assert first.post(photo + "/equation", json=body).status_code == 409
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM problem_submissions").fetchone()[0] == before
        assert connection.execute("SELECT COUNT(*) FROM photo_equation_sources").fetchone()[0] == 0
    assert first.get(photo).json()["latest_input"]["content_text"] == PHOTO_OCR_TEXT


def test_logout_revokes_a_cookie_even_when_it_is_replayed(students):
    app, first, _, _, _ = students
    token = first.cookies.get(SESSION_COOKIE)
    assert token
    assert first.post("/auth/logout").status_code == 204
    assert first.get("/me").status_code == 401
    assert first.get("/submissions").status_code == 401
    replay = TestClient(app)
    try:
        replay.cookies.set(SESSION_COOKIE, token)
        assert replay.get("/me").status_code == 401
        assert replay.post("/submissions/text", json={"content_text": "x=6"}).status_code == 401
    finally:
        replay.close()


def test_expired_session_blocks_profile_and_submission(database, students):
    _, first, _, _, _ = students
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("UPDATE auth_sessions SET expires_at=? WHERE token_hash=?",
                           (int(time.time()) - 1, token_digest(first.cookies.get(SESSION_COOKIE))))
        connection.commit()
    assert first.get("/me").status_code == 401
    assert first.get("/submissions").status_code == 401
    assert first.post("/submissions/text", json={"content_text": "x=6"}).status_code == 401


def test_deleted_account_does_not_keep_submission_access(database, students):
    _, first, _, first_id, _ = students
    create_problem(first)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("DELETE FROM users WHERE id=?", (first_id,))
        connection.commit()
    assert first.get("/me").status_code == 401
    assert first.get("/submissions").status_code == 401
    assert first.post("/submissions/text", json={"content_text": "x=6"}).status_code == 401


def test_switching_accounts_does_not_restore_previous_students_records(database):
    with TestClient(make_app(database)) as client:
        register_and_login(client, "first@example.com")
        submission = create_problem(client)
        assert client.post("/auth/logout").status_code == 204
        register_and_login(client, "second@example.com")
        assert client.get("/submissions").json() == []
        assert client.get(f"/submissions/{submission['id']}").status_code == 404


def test_session_and_solution_records_restore_after_app_restart(database):
    first_app = make_app(database)
    with TestClient(first_app) as first:
        user_id = register_and_login(first, "first@example.com")
        token = first.cookies.get(SESSION_COOKIE)
        submission = create_problem(first)
        result, viewed = verify_problem(first, submission["id"])
    assert first_app.state.ocr_jobs.executor is None
    second_app = make_app(database)
    with TestClient(second_app) as restored:
        restored.cookies.set(SESSION_COOKIE, token)
        assert restored.get("/me").json()["user_id"] == user_id
        assert [row["id"] for row in restored.get("/submissions").json()] == [submission["id"]]
        base = f"/submissions/{submission['id']}"
        assert restored.get(base + "/verification").json() == result
        assert restored.get(base + "/feedback/history").json() == [viewed]
