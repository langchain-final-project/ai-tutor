# 별도 개발 DB 생성·재실행·업그레이드와 기존 파일 보호를 검사

from contextlib import closing
import importlib.util
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/setup_solution_flow.py"
spec = importlib.util.spec_from_file_location("solution_setup", SCRIPT)
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


def create_base(path):
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for name, migration_id in setup.BASE:
            connection.executescript(setup._schema(name))
            if migration_id:
                connection.execute(
                    "INSERT INTO problem_schema_migrations(id,schema_sha256) VALUES (?,?)",
                    (migration_id, setup._digest(setup._schema(name))),
                )
                connection.commit()
        connection.execute("INSERT INTO problem_submissions(owner_id,input_kind) VALUES ('student_A','text')")
        connection.execute("INSERT INTO problem_input_versions(submission_id,version_number,origin,content_text) VALUES (1,1,'student','x=2')")
        connection.commit()


def test_create_requires_explicit_flag(tmp_path):
    target = tmp_path / "new.sqlite3"
    with pytest.raises(ValueError, match="create-empty"):
        setup.prepare(target)
    assert not target.exists()


def test_create_and_repeat_keeps_data(tmp_path):
    target = tmp_path / "new.sqlite3"
    assert setup.prepare(target, create_empty=True)["action"] == "created_empty_development_database"
    with closing(sqlite3.connect(target)) as connection:
        connection.execute("INSERT INTO problem_submissions(owner_id,input_kind) VALUES ('student_A','text')")
        connection.commit()
    assert setup.prepare(target, create_empty=True)["action"] == "upgrade_or_verify"
    with closing(sqlite3.connect(target)) as connection:
        assert connection.execute("SELECT owner_id FROM problem_submissions").fetchall() == [("student_A",)]
        assert connection.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_with_auth_cli_repeat_preserves_accounts_sessions_and_questions(tmp_path):
    target = tmp_path / "student.sqlite3"
    command = [sys.executable, str(SCRIPT), "--database", str(target),
               "--create-empty", "--with-auth"]
    subprocess.run(command, check=True, capture_output=True, text=True)
    with closing(sqlite3.connect(target)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("""INSERT INTO users(id,name,email,password_hash,created_at)
            VALUES ('student_A','학생','student@example.com','test-hash',CURRENT_TIMESTAMP)""")
        connection.execute("""INSERT INTO auth_sessions(token_hash,user_id,expires_at)
            VALUES ('test-token-hash','student_A',2147483647)""")
        connection.execute("INSERT INTO problem_submissions(owner_id,input_kind) VALUES ('student_A','text')")
        connection.commit()
    subprocess.run(command, check=True, capture_output=True, text=True)
    with closing(sqlite3.connect(target)) as connection:
        assert connection.execute("SELECT id FROM users").fetchall() == [("student_A",)]
        assert connection.execute("SELECT token_hash,user_id FROM auth_sessions").fetchall() == [
            ("test-token-hash", "student_A")]
        assert connection.execute("SELECT owner_id FROM problem_submissions").fetchall() == [("student_A",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_upgrade_existing_base_preserves_question(tmp_path):
    target = tmp_path / "old.sqlite3"
    create_base(target)
    setup.prepare(target)
    with closing(sqlite3.connect(target)) as connection:
        assert connection.execute("SELECT content_text FROM problem_input_versions").fetchall() == [("x=2",)]
        assert connection.execute("SELECT id FROM problem_schema_migrations WHERE id=?", (setup.MIGRATION_ID,)).fetchone()


def test_upgrade_previous_solution_schema_preserves_checksum_and_records(tmp_path):
    target = tmp_path / "previous-solution.sqlite3"
    create_base(target)
    original_digest = setup._digest(setup._schema(setup.SCHEMA_FILE))
    with closing(sqlite3.connect(target)) as connection:
        connection.executescript(setup._schema(setup.SCHEMA_FILE))
        connection.execute(
            "INSERT INTO problem_schema_migrations(id,schema_sha256) VALUES (?,?)",
            (setup.MIGRATION_ID, original_digest),
        )
        connection.execute("""INSERT INTO student_work_versions
            (submission_id,problem_input_version_id,version_number,steps_json)
            VALUES (1,1,1,'[]')""")
        connection.commit()
    setup.prepare(target)
    setup.prepare(target)
    with closing(sqlite3.connect(target)) as connection:
        assert connection.execute("SELECT content_text FROM problem_input_versions").fetchall() == [("x=2",)]
        assert connection.execute("SELECT version_number,steps_json FROM student_work_versions").fetchall() == [(1, "[]")]
        assert connection.execute("SELECT schema_sha256 FROM problem_schema_migrations WHERE id=?",
                                  (setup.MIGRATION_ID,)).fetchone() == (original_digest,)
        assert "selected_region_json" in {row[1] for row in connection.execute("PRAGMA table_info(problem_ocr_jobs)")}
        assert connection.execute("SELECT schema_sha256 FROM problem_schema_migrations WHERE id=?",
                                  (setup.SOURCE_MIGRATION_ID,)).fetchone() == (setup._digest(setup._schema(setup.SOURCE_SCHEMA_FILE)),)


def test_upgrade_team_region_schema_preserves_existing_job_and_repeats(tmp_path):
    target = tmp_path / "team-regions.sqlite3"
    create_base(target)
    with closing(sqlite3.connect(target)) as connection:
        connection.executescript(setup._schema(setup.REGION_SCHEMA_FILE))
        connection.execute("""INSERT INTO uploaded_problems
            (original_filename,mime_type,image_sha256,image_content)
            VALUES ('photo.png','image/png',?,?)""", ("a" * 64, b"photo"))
        photo_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        connection.execute("INSERT INTO problem_submissions(owner_id,input_kind,uploaded_problem_id) VALUES ('student_A','photo',?)", (photo_id,))
        submission_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        connection.execute("INSERT INTO problem_ocr_jobs(id,submission_id,selected_region_json) VALUES ('team-job',?,'{}')", (submission_id,))
        connection.commit()
    setup.prepare(target)
    setup.prepare(target)
    with closing(sqlite3.connect(target)) as connection:
        assert connection.execute("SELECT id,selected_region_json FROM problem_ocr_jobs").fetchall() == [("team-job", "{}")]
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE problem_ocr_jobs SET selected_region_json=NULL")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_partial_region_schema_is_rejected_without_adding_solution_objects(tmp_path):
    target = tmp_path / "partial-region.sqlite3"
    create_base(target)
    with closing(sqlite3.connect(target)) as connection:
        connection.execute("ALTER TABLE problem_ocr_jobs ADD COLUMN selected_region_json TEXT")
    with pytest.raises(ValueError, match="OCR 영역 스키마가 일부"):
        setup.prepare(target)
    with closing(sqlite3.connect(target)) as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='student_work_versions'").fetchone() is None


def test_changed_region_trigger_is_rejected(tmp_path):
    target = tmp_path / "changed-region.sqlite3"
    setup.prepare(target, create_empty=True)
    with closing(sqlite3.connect(target)) as connection:
        connection.execute("DROP TRIGGER trg_ocr_job_region_immutable")
        connection.executescript("""CREATE TRIGGER trg_ocr_job_region_immutable
            BEFORE UPDATE OF selected_region_json ON problem_ocr_jobs BEGIN SELECT 1; END;""")
    with pytest.raises(ValueError, match="불변 트리거"):
        setup.prepare(target)


def test_new_migration_checksum_mismatch_is_rejected(tmp_path):
    target = tmp_path / "changed-source.sqlite3"
    setup.prepare(target, create_empty=True)
    with closing(sqlite3.connect(target)) as connection:
        connection.execute("UPDATE problem_schema_migrations SET schema_sha256=? WHERE id=?",
                           ("0" * 64, setup.SOURCE_MIGRATION_ID))
        connection.commit()
    with pytest.raises(ValueError, match="007 SQL"):
        setup.prepare(target)


@pytest.mark.parametrize("existing_solution", [False, True])
def test_failed_extension_rolls_back_region_and_source_schema(tmp_path, monkeypatch, existing_solution):
    target = tmp_path / "extension-rollback.sqlite3"
    create_base(target)
    if existing_solution:
        with closing(sqlite3.connect(target)) as connection:
            connection.executescript(setup._schema(setup.SCHEMA_FILE))
            connection.execute("INSERT INTO problem_schema_migrations(id,schema_sha256) VALUES (?,?)",
                               (setup.MIGRATION_ID, setup._digest(setup._schema(setup.SCHEMA_FILE))))
            connection.commit()
    original = setup._verify_objects

    def fail_after_extensions(connection, expected, columns):
        original(connection, expected, columns)
        if "photo_equation_sources" in expected:
            raise ValueError("합성 사진 출처 적용 실패")

    monkeypatch.setattr(setup, "_verify_objects", fail_after_extensions)
    with pytest.raises(ValueError, match="합성 사진 출처"):
        setup.prepare(target)
    with closing(sqlite3.connect(target)) as connection:
        assert "selected_region_json" not in {row[1] for row in connection.execute("PRAGMA table_info(problem_ocr_jobs)")}
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='photo_equation_sources'").fetchone() is None
        assert (connection.execute("SELECT name FROM sqlite_master WHERE name='student_work_versions'").fetchone() is not None) == existing_solution
        assert connection.execute("SELECT content_text FROM problem_input_versions").fetchall() == [("x=2",)]
        assert connection.execute("SELECT id FROM problem_schema_migrations WHERE id=?",
                                  (setup.SOURCE_MIGRATION_ID,)).fetchone() is None
        assert (connection.execute("SELECT id FROM problem_schema_migrations WHERE id=?",
                                   (setup.MIGRATION_ID,)).fetchone() is not None) == existing_solution


def test_photo_equation_source_enforces_confirmed_photo_owner_and_immutability(tmp_path):
    target = tmp_path / "source-constraints.sqlite3"
    create_base(target)
    setup.prepare(target)
    with closing(sqlite3.connect(target)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")

        def photo(owner, confirmed):
            image_id = connection.execute("""INSERT INTO uploaded_problems
                (original_filename,mime_type,image_sha256,image_content)
                VALUES ('photo.png','image/png',?,?)""", (setup._digest(owner + str(confirmed)), b"photo")).lastrowid
            submission_id = connection.execute("""INSERT INTO problem_submissions
                (owner_id,input_kind,uploaded_problem_id) VALUES (?,'photo',?)""", (owner, image_id)).lastrowid
            input_id = connection.execute("""INSERT INTO problem_input_versions
                (submission_id,version_number,origin,content_text) VALUES (?,1,'student','원문')""", (submission_id,)).lastrowid
            if confirmed:
                connection.execute("UPDATE problem_input_versions SET confirmed_at=CURRENT_TIMESTAMP WHERE id=?", (input_id,))
            return submission_id, input_id

        source, source_input = photo("student_A", True)
        other, other_input = photo("student_B", True)
        unconfirmed, unconfirmed_input = photo("student_A", False)
        other_target = connection.execute("INSERT INTO problem_submissions(owner_id,input_kind) VALUES ('student_B','text')").lastrowid
        invalid = [
            (other_target, source, source_input),
            (source, source, source_input),
            (1, 1, 1),
            (1, unconfirmed, unconfirmed_input),
            (1, source, other_input),
        ]
        for values in invalid:
            with pytest.raises(sqlite3.IntegrityError, match="confirmed photo"):
                connection.execute("""INSERT INTO photo_equation_sources
                    (submission_id,source_submission_id,source_input_version_id) VALUES (?,?,?)""", values)
        connection.execute("""INSERT INTO photo_equation_sources
            (submission_id,source_submission_id,source_input_version_id) VALUES (?,?,?)""", (1, source, source_input))
        assert connection.execute("SELECT source_submission_id,source_input_version_id FROM photo_equation_sources").fetchone() == (source, source_input)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE photo_equation_sources SET source_submission_id=?", (other,))
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_existing_unknown_file_is_not_overwritten(tmp_path):
    target = tmp_path / "unknown.sqlite3"
    target.write_bytes(b"not a sqlite database")
    before = target.read_bytes()
    with pytest.raises(sqlite3.DatabaseError):
        setup.prepare(target, create_empty=True)
    assert target.read_bytes() == before


def test_missing_base_tables_rejects_without_adding_schema(tmp_path):
    target = tmp_path / "partial.sqlite3"
    with closing(sqlite3.connect(target)) as connection:
        connection.execute("CREATE TABLE placeholder(id INTEGER)")
    with pytest.raises(ValueError, match="DB 객체 누락"):
        setup.prepare(target)
    with closing(sqlite3.connect(target)) as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("placeholder",)]


def test_recorded_checksum_mismatch_is_rejected(tmp_path):
    target = tmp_path / "changed.sqlite3"
    setup.prepare(target, create_empty=True)
    with closing(sqlite3.connect(target)) as connection:
        connection.execute("UPDATE problem_schema_migrations SET schema_sha256=? WHERE id=?", ("0" * 64, setup.MIGRATION_ID))
        connection.commit()
    with pytest.raises(ValueError, match="변경되었습니다"):
        setup.prepare(target)


def test_failed_upgrade_rolls_back_new_objects_and_keeps_inputs(tmp_path, monkeypatch):
    target = tmp_path / "rollback.sqlite3"
    create_base(target)
    original = setup._verify_objects
    def fail_new_schema(connection, expected, columns):
        original(connection, expected, columns)
        if "student_work_versions" in expected:
            raise ValueError("합성 적용 후 검사 실패")
    monkeypatch.setattr(setup, "_verify_objects", fail_new_schema)
    with pytest.raises(ValueError, match="합성 적용 후"):
        setup.prepare(target)
    with closing(sqlite3.connect(target)) as connection:
        assert connection.execute("SELECT content_text FROM problem_input_versions").fetchall() == [("x=2",)]
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='student_work_versions'").fetchone() is None
        assert connection.execute("SELECT id FROM problem_schema_migrations WHERE id=?", (setup.MIGRATION_ID,)).fetchone() is None


def test_new_db_failure_leaves_no_destination_or_temp_file(tmp_path, monkeypatch):
    target = tmp_path / "failed.sqlite3"
    def fail(_path):
        raise ValueError("합성 생성 실패")
    monkeypatch.setattr(setup, "_build_empty", fail)
    with pytest.raises(ValueError, match="합성 생성 실패"):
        setup.prepare(target, create_empty=True)
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_destination_created_during_build_is_not_overwritten(tmp_path, monkeypatch):
    target = tmp_path / "race.sqlite3"
    original = setup._build_empty
    def create_competing_file(path):
        original(path)
        target.write_bytes(b"another process")
    monkeypatch.setattr(setup, "_build_empty", create_competing_file)
    with pytest.raises(FileExistsError):
        setup.prepare(target, create_empty=True)
    assert target.read_bytes() == b"another process"
    assert list(tmp_path.iterdir()) == [target]


def test_demo_can_use_injected_transport_without_network():
    script = SCRIPT.with_name("demo_solution_flow.py")
    demo_spec = importlib.util.spec_from_file_location("solution_demo", script)
    demo = importlib.util.module_from_spec(demo_spec)
    demo_spec.loader.exec_module(demo)
    calls = []
    evaluation = {"verification": {"judgement": "correct", "completion": "complete"},
                  "model_route": {"model_required": False}, "student_feedback": {"hint": "확인 안내"}}
    result = {"id": 10, "is_current": True, "evaluation": evaluation}
    responses = [(201, {"id": 1, "latest_input": {"version_number": 1}}),
                 (200, {}), (201, {"version_number": 1}), (200, {}),
                 (201, result), (200, result), (200, {})]
    def request(method, path, payload=None):
        calls.append((method, path, payload))
        return responses[len(calls)-1]
    logs = []
    assert demo.run_flow(request, output=logs.append) == result
    assert len(calls) == 7
    assert calls[-1][1] == "/submissions/1/verification/10/feedback-viewed"
    assert "확인 안내" in logs[-1]


def test_demo_rejects_nonlocal_server():
    script = SCRIPT.with_name("demo_solution_flow.py")
    demo_spec = importlib.util.spec_from_file_location("solution_demo_urls", script)
    demo = importlib.util.module_from_spec(demo_spec)
    demo_spec.loader.exec_module(demo)
    with pytest.raises(ValueError, match="로컬"):
        demo.http_transport("https://example.com", "unused")
