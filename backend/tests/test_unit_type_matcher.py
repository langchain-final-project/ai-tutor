"""학기·단원 제한, 숫자 보존, 후보 근거, 확인된 입력 버전과의 연결을 검사한다."""

from contextlib import closing
from pathlib import Path
import sqlite3

from fastapi.testclient import TestClient
import pytest

from app.db.submissions import create_text_submission, save_student_input_version, submission_connection
from app import submission_api
from app.submission_api import create_submission_app
from app.tutor.unit_type_matcher import UnitTypeMatcher, canonical, equation_focus


def rows():
    examples = [
        ("7103047", "간단한 일차방정식의 풀이", "일차방정식 3x-2=7을 풀어라."),
        ("7103048", "괄호가 있는 일차방정식의 풀이", "일차방정식 3(x-2)=2x+5를 풀어라."),
        ("7103049", "계수에 소수가 있는 일차방정식의 풀이", "일차방정식 0.2(x-3)-0.4(x-4)=0을 풀면?"),
        ("7103050", "계수에 분수가 있는 일차방정식의 풀이", r"일차방정식 \frac{x}{3}-\frac{2}{5}=1을 풀어라."),
        ("7103016", "간단한 식의 값 구하기", "x=2일 때 3x-1의 값을 구하여라."),
    ]
    return [{"id": f"unit-{index}", "question_text": text, "figure_text": None,
             "source_topic_code": code, "source_topic_name": name}
            for index, (code, name, text) in enumerate(examples)]


@pytest.fixture
def matcher():
    return UnitTypeMatcher(rows())


def test_canonical_keeps_decimal_sign_exponent_and_variable():
    assert canonical("0.2x=-3") == "0.2x=-3"
    assert canonical("18\n일차방정식 \\(0.2x=-3\\)") == "일차방정식0.2x=-3"
    assert canonical("x²=-3") == "x^2=-3"
    assert canonical("x=-3") != canonical("x=3")
    assert canonical("x^2=3") != canonical("x=3")


def test_exact_body_match_returns_bank_type_and_reference(matcher):
    result = matcher.predict("일차방정식 \\(3x-2=7\\)을 풀어라.")
    assert result["status"] == "classified"
    assert result["method"] == "exact_match"
    assert result["selected_type"]["source_topic_code"] == "7103047"
    assert result["matched_question_id"] == "unit-0"
    assert result["candidates"][0]["reference_questions"][0]["question_text"] == rows()[0]["question_text"]


@pytest.mark.parametrize("text,code", [
    ("일차방정식 0.3(x-5)-0.7(x-2)=0을 풀면?", "7103049"),
    (r"일차방정식 \frac{x}{4}-\frac{2}{7}=3을 풀어라.", "7103050"),
    ("일차방정식 7(x-2)=3x+5를 풀어라.", "7103048"),
    ("일차방정식 7x-2=19를 풀어라.", "7103047"),
])
def test_structural_equation_ranking_does_not_auto_confirm(matcher, text, code):
    result = matcher.predict(text)
    assert result["status"] == "candidates"
    assert result["selected_type"] is None
    assert result["candidates"][0]["source_topic_code"] == code
    assert len(result["candidates"]) <= 3


def test_choices_and_parameter_target_do_not_override_question_goal():
    code, _ = equation_focus("일차방정식 2x=6을 풀어라. ①0.5 ②0.7")
    assert code == "7103047"
    assert equation_focus("일차방정식 ax=3을 풀었을 때 해가 2이다. 상수 a의 값을 구하여라.")[0] is None
    assert equation_focus(r"다음 일차방정식을 풀어라.(1) $2x=6$ (2) $3x=9$")[0] == "7103047"
    assert equation_focus(r"다음을 계산하여라. $(5a+3)\div\frac{3}{2}$")[0] == "7103025"
    assert equation_focus(r"다음 중 옳지 않은 것은? ① $a\times b=ab$ ② $a\times2=2a$")[0] == "7103002"
    assert equation_focus(r"다음을 계산하여라. $(-3)\div2$")[0] is None
    assert equation_focus(r"다음 중 항등식인 것은? ① $a\times b=ab$")[0] is None


@pytest.mark.parametrize("text,status", [
    ("[UNREADABLE]", "needs_review"), ("다음 중 옳은 것은?", "needs_review"),
    ("🦄🦄", "needs_review"), ("이차방정식 x^2=9를 풀어라.", "unsupported"),
    ("두 식의 연립방정식을 풀어라.", "unsupported"),
])
def test_unusable_or_outside_inputs_have_no_forced_type(matcher, text, status):
    result = matcher.predict(text)
    assert result["status"] == status
    assert result["selected_type"] is None
    assert result["candidates"] == []


def test_duplicate_body_with_different_labels_and_visuals_stays_candidates(matcher):
    assert matcher.predict(rows()[0]["question_text"], has_visuals=True)["status"] == "candidates"
    duplicate = {**rows()[0], "id": "ambiguous", "source_topic_code": "7103048", "source_topic_name": "다른 유형"}
    result = UnitTypeMatcher([*rows(), duplicate]).predict(rows()[0]["question_text"])
    assert result["status"] == "candidates"
    assert len(result["candidates"]) == 2


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "types.sqlite3"
    migrations = Path(__file__).resolve().parents[1] / "migrations"
    with closing(sqlite3.connect(path)) as connection:
        for name in ("001_problem_bank.sqlite.sql", "002_problem_classification.sqlite.sql",
                     "004_problem_submissions.sqlite.sql", "005_problem_ocr_jobs.sqlite.sql"):
            connection.executescript((migrations / name).read_text(encoding="utf-8"))
    return path


@pytest.fixture
def bank(tmp_path):
    path = tmp_path / "bank.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript("""
            CREATE TABLE topics(source_topic_code TEXT PRIMARY KEY,source_topic_name TEXT);
            CREATE TABLE questions(id TEXT PRIMARY KEY,grade INTEGER,semester INTEGER,unit_code TEXT,
                question_text TEXT,figure_text TEXT,source_topic_code TEXT,answer TEXT);
        """)
        for row in rows():
            connection.execute("INSERT INTO topics VALUES(?,?)", (row["source_topic_code"], row["source_topic_name"]))
            connection.execute("INSERT INTO questions VALUES(?,1,1,'03',?,?,?,'SECRET_ANSWER')",
                               (row["id"], row["question_text"], row["figure_text"], row["source_topic_code"]))
        connection.execute("INSERT INTO topics VALUES('8101001','범위 밖')")
        for index, (grade, semester, unit) in enumerate([(2, 1, "03"), (1, 2, "03"), (1, 1, "02")]):
            connection.execute("INSERT INTO questions VALUES(?,?,?,?,?,NULL,'8101001','SECRET_ANSWER')",
                               (f"outside-{index}", grade, semester, unit, rows()[0]["question_text"]))
        connection.commit()
    return path


def test_bank_reader_enforces_grade_semester_unit_and_excludes_answers(bank):
    before = bank.read_bytes()
    matcher = UnitTypeMatcher.load(bank)
    result = matcher.predict(rows()[0]["question_text"])
    assert len(matcher.rows) == 5
    assert all(candidate["source_topic_code"] != "8101001" for candidate in result["candidates"])
    assert "SECRET_ANSWER" not in str(result)
    assert bank.read_bytes() == before


HEADERS = {"Authorization": "Bearer type-test"}


def create_input(database, text="일차방정식 7x-2=19를 풀어라."):
    with submission_connection(database) as connection:
        return create_text_submission(connection, owner_id="A", content_text=text)["submission_id"]


def make_app(database, bank, owner="A"):
    return create_submission_app(database, dev_token="type-test", owner_id=owner, type_bank_database=bank)


def confirm(client, sid, text="일차방정식 7x-2=19를 풀어라.", version=1):
    response = client.post(f"/submissions/{sid}/confirm", headers=HEADERS,
                           json={"content_text": text, "expected_version_number": version})
    assert response.status_code == 200, response.text
    return response.json()


def test_classification_requires_confirmation_and_current_version(database, bank):
    sid = create_input(database)
    with TestClient(make_app(database, bank)) as client:
        endpoint = f"/submissions/{sid}/type-match"
        assert client.post(endpoint, headers=HEADERS, json={"expected_version_number": 1}).status_code == 409
        confirm(client, sid)
        assert client.post(endpoint, headers=HEADERS, json={"expected_version_number": 2}).status_code == 409
        response = client.post(endpoint, headers=HEADERS, json={"expected_version_number": 1})
        assert response.status_code == 200, response.text
        assert response.json()["candidates"][0]["source_topic_code"] == "7103047"
        assert response.json()["input_version_number"] == 1
        assert client.post(endpoint, json={"expected_version_number": 1}).status_code == 401
        assert client.post(endpoint, headers=HEADERS, json={"expected_version_number": 1, "question_text": "injected"}).status_code == 422
    with TestClient(make_app(database, bank, owner="B")) as client:
        assert client.post(endpoint, headers=HEADERS, json={"expected_version_number": 1}).status_code == 404


def test_type_selection_restores_and_becomes_stale_after_edit(database, bank):
    sid = create_input(database)
    with TestClient(make_app(database, bank)) as client:
        confirm(client, sid)
        endpoint = f"/submissions/{sid}/type-match"
        found = client.post(endpoint, headers=HEADERS, json={"expected_version_number": 1}).json()
        code = found["candidates"][0]["source_topic_code"]
        assert client.post(endpoint + "/confirm", headers=HEADERS,
                           json={"expected_version_number": 1, "source_topic_code": "8101001"}).status_code == 422
        chosen = client.post(endpoint + "/confirm", headers=HEADERS,
                             json={"expected_version_number": 1, "source_topic_code": code})
        assert chosen.status_code == 200
        assert chosen.json()["status"] == "classified"
        assert chosen.json()["method"] == "manual"
        assert client.post(endpoint, headers=HEADERS, json={"expected_version_number": 1}).json()["method"] == "manual"
    with TestClient(make_app(database, bank)) as client:
        restored = client.get(f"/submissions/{sid}", headers=HEADERS).json()
        assert restored["type_match"]["selected_type"]["source_topic_code"] == code
        edited = client.patch(f"/submissions/{sid}/input", headers=HEADERS,
                              json={"expected_version_number": 1, "content_text": "일차방정식 0.3x=9를 풀어라."})
        assert edited.status_code == 200
        assert edited.json()["type_match"] is None
        assert client.post(endpoint, headers=HEADERS, json={"expected_version_number": 1}).status_code == 409
        confirm(client, sid, "일차방정식 0.3x=9를 풀어라.", 2)
        new = client.post(endpoint, headers=HEADERS, json={"expected_version_number": 2}).json()
        assert new["candidates"][0]["source_topic_code"] == "7103049"
        with closing(sqlite3.connect(database)) as connection:
            assert connection.execute("SELECT count(DISTINCT input_version_id) FROM problem_input_type_matches").fetchone()[0] == 2


def test_missing_bank_is_reported_after_input_confirmation(database, tmp_path):
    sid = create_input(database)
    with TestClient(make_app(database, tmp_path / "missing.sqlite3")) as client:
        confirm(client, sid)
        response = client.post(f"/submissions/{sid}/type-match", headers=HEADERS,
                               json={"expected_version_number": 1})
        assert response.status_code == 503
        assert not (tmp_path / "missing.sqlite3").exists()


def test_edit_during_matching_cannot_save_stale_result(database, bank, monkeypatch):
    sid = create_input(database)
    original = UnitTypeMatcher.load(bank)

    class ConcurrentEdit:
        data_version = original.data_version

        def predict(self, text, **kwargs):
            with submission_connection(database) as connection:
                save_student_input_version(connection, submission_id=sid, owner_id="A",
                                           expected_version_number=1, content_text="문제 내용을 수정했습니다.")
            return original.predict(text)

    monkeypatch.setattr(submission_api, "get_unit_matcher", lambda path: ConcurrentEdit())
    with TestClient(make_app(database, bank)) as client:
        confirm(client, sid)
        response = client.post(f"/submissions/{sid}/type-match", headers=HEADERS,
                               json={"expected_version_number": 1})
        assert response.status_code == 409
        assert client.get(f"/submissions/{sid}", headers=HEADERS).json()["type_match"] is None
        with closing(sqlite3.connect(database)) as connection:
            assert connection.execute("SELECT count(*) FROM problem_input_type_matches").fetchone()[0] == 0
