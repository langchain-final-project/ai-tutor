# 별도 SQLite 개발 DB를 준비하거나 기존 DB에 풀이 저장 구조를 추가

# 실행: python backend/scripts/setup_solution_flow.py --create-empty
# 새 DB는 문제은행 문항이 없는 개발용으로 기존 파일은 덮어쓰지 않는 형태

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
DATABASE = ROOT / "data/local/solution-flow.sqlite3"
BASE = (
    ("001_problem_bank.sqlite.sql", None),
    ("002_problem_classification.sqlite.sql", "problem-classification-v1"),
    ("004_problem_submissions.sqlite.sql", "problem-submissions-v1"),
    ("005_problem_ocr_jobs.sqlite.sql", "problem-ocr-jobs-v1"),
)
SCHEMA_FILE = "006_solution_verification.sqlite.sql"
MIGRATION_ID = "solution-verification-v1"
REGION_SCHEMA_FILE = "006_problem_ocr_regions.sqlite.sql"
REGION_TRIGGER = "trg_ocr_job_region_immutable"
SOURCE_SCHEMA_FILE = "007_photo_equation_sources.sqlite.sql"
SOURCE_MIGRATION_ID = "photo-equation-sources-v1"
TYPE_SCHEMA_FILE = "007_problem_input_type_matches.sqlite.sql"
TYPE_MIGRATION_ID = "problem-input-type-matches-v1"
TUTOR_SCHEMA_FILE = "008_tutor_sessions.sqlite.sql"
TUTOR_MIGRATION_ID = "tutor-sessions-v1"
LEARNING_SCHEMA_FILE = "009_learning_paths.sqlite.sql"
LEARNING_MIGRATION_ID = "learning-paths-v1"


def _schema(name):
    return (MIGRATIONS / name).read_text(encoding="utf-8")


def _digest(schema):
    return hashlib.sha256(schema.encode("utf-8")).hexdigest()


def _objects(connection):
    return dict(connection.execute(
        "SELECT name, type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
    ))


def _reference(*, with_regions=False, with_sources=False, with_types=False, with_tutor=False,
               with_learning=False):
    with closing(sqlite3.connect(":memory:")) as reference:
        for name, _ in BASE:
            reference.executescript(_schema(name))
        base = _objects(reference)
        reference.executescript(_schema(SCHEMA_FILE))
        if with_regions:
            reference.executescript(_schema(REGION_SCHEMA_FILE))
        if with_sources:
            reference.executescript(_schema(SOURCE_SCHEMA_FILE))
        if with_types:
            reference.executescript(_schema(TYPE_SCHEMA_FILE))
        if with_tutor:
            reference.executescript(_schema(TUTOR_SCHEMA_FILE))
        if with_learning:
            reference.executescript(_schema(LEARNING_SCHEMA_FILE))
        full = _objects(reference)
        columns = {
            name: list(reference.execute(f'PRAGMA table_info("{name}")'))
            for name, kind in full.items() if kind == "table"
        }
        return base, full, columns


def _verify_objects(connection, expected, columns):
    actual = _objects(connection)
    for name, kind in expected.items():
        if actual.get(name) != kind:
            raise ValueError(f"DB 객체 누락: {name}")
        if kind == "table":
            current = list(connection.execute(f'PRAGMA table_info("{name}")'))
            if current != columns[name]:
                raise ValueError(f"지원하지 않는 테이블 구조: {name}")


def _verify_integrity(connection):
    if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise ValueError("SQLite 무결성 검사 실패")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise ValueError("외래키 검사 실패")


def _verify_history(connection):
    for name, migration_id in BASE:
        if migration_id is None:
            continue
        row = connection.execute(
            "SELECT schema_sha256 FROM problem_schema_migrations WHERE id = ?",
            (migration_id,),
        ).fetchone()
        if row != (_digest(_schema(name)),):
            raise ValueError(f"먼저 기존 마이그레이션을 확인해 주세요: {migration_id}")


def _has_region_schema(connection):
    columns = {row[1] for row in connection.execute("PRAGMA table_info(problem_ocr_jobs)")}
    trigger = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (REGION_TRIGGER,),
    ).fetchone()
    has_column = "selected_region_json" in columns
    if has_column != (trigger is not None):
        raise ValueError("OCR 영역 스키마가 일부만 존재합니다. 마이그레이션 상태를 확인해 주세요.")
    if trigger is not None:
        expected = "CREATE TRIGGER" + _schema(REGION_SCHEMA_FILE).split("CREATE TRIGGER", 1)[1]
        if trigger[0].strip().rstrip(";") != expected.strip().rstrip(";"):
            raise ValueError("OCR 영역 불변 트리거의 내용이 변경되었습니다.")
    return has_column


def _has_type_match_schema(connection):
    """팀원 API가 먼저 만든 유형 저장소도 구조를 확인한 뒤 이력에 포함한다."""
    with closing(sqlite3.connect(":memory:")) as reference:
        reference.executescript(_schema(TYPE_SCHEMA_FILE))
        expected = _objects(reference)
        present = set(expected).intersection(_objects(connection))
        if not present:
            return False
        if present != set(expected):
            raise ValueError("유형 저장 스키마가 일부만 존재합니다. 적용 상태를 확인해 주세요.")
        columns = {"problem_input_type_matches": list(reference.execute(
            "PRAGMA table_info(problem_input_type_matches)"
        ))}
        _verify_objects(connection, expected, columns)
        for pragma in ("foreign_key_list(problem_input_type_matches)",
                       "index_info(ix_input_type_match_version)"):
            if list(connection.execute(f"PRAGMA {pragma}")) != list(reference.execute(f"PRAGMA {pragma}")):
                raise ValueError("유형 저장소의 외래키 또는 인덱스 구조가 변경되었습니다.")
    return True


def _has_strict_schema(connection, schema_file, label):
    """부분 적용·변조 상태를 먼저 검사해 기존 기록을 덮어쓰지 않는다."""
    with closing(sqlite3.connect(":memory:")) as reference:
        reference.executescript(_schema(schema_file))
        expected = _objects(reference)
        present = set(expected).intersection(_objects(connection))
        if not present:
            return False
        if present != set(expected):
            raise ValueError(f"{label} 저장 스키마가 일부만 존재합니다. 적용 상태를 확인해 주세요.")
        for name in expected:
            actual = connection.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
            wanted = reference.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
            if actual != wanted:
                raise ValueError(f"{label} 저장 스키마 내용이 변경되었습니다.")
    return True


def _has_tutor_schema(connection):
    return _has_strict_schema(connection, TUTOR_SCHEMA_FILE, "튜터")


def apply_migration(connection):
    """풀이·OCR 영역·사진 출처·유형 저장소를 원자적으로 적용한다."""
    if connection.in_transaction:
        raise ValueError("진행 중인 저장 작업을 마친 뒤 마이그레이션해 주세요.")
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise ValueError("외래키 검사를 활성화해 주세요.")
    base, full, columns = _reference()
    has_regions = _has_region_schema(connection)
    has_types = _has_type_match_schema(connection)
    has_tutor = _has_tutor_schema(connection)
    has_learning = _has_strict_schema(connection, LEARNING_SCHEMA_FILE, "학습 경로")
    _, extended, extended_columns = _reference(with_regions=True, with_sources=True, with_types=True,
                                               with_tutor=True, with_learning=True)
    _, sources, _ = _reference(with_sources=True)
    current_columns = dict(extended_columns)
    if not has_regions:
        current_columns["problem_ocr_jobs"] = columns["problem_ocr_jobs"]
    _verify_objects(connection, base, current_columns)
    _verify_history(connection)
    _verify_integrity(connection)
    schema = _schema(SCHEMA_FILE)
    digest = _digest(schema)
    row = connection.execute(
        "SELECT schema_sha256 FROM problem_schema_migrations WHERE id = ?",
        (MIGRATION_ID,),
    ).fetchone()
    source_schema = _schema(SOURCE_SCHEMA_FILE)
    source_digest = _digest(source_schema)
    source_row = connection.execute(
        "SELECT schema_sha256 FROM problem_schema_migrations WHERE id = ?",
        (SOURCE_MIGRATION_ID,),
    ).fetchone()
    type_schema = _schema(TYPE_SCHEMA_FILE)
    type_digest = _digest(type_schema)
    type_row = connection.execute(
        "SELECT schema_sha256 FROM problem_schema_migrations WHERE id = ?",
        (TYPE_MIGRATION_ID,),
    ).fetchone()
    tutor_schema = _schema(TUTOR_SCHEMA_FILE)
    tutor_digest = _digest(tutor_schema)
    tutor_row = connection.execute(
        "SELECT schema_sha256 FROM problem_schema_migrations WHERE id = ?",
        (TUTOR_MIGRATION_ID,),
    ).fetchone()
    learning_schema = _schema(LEARNING_SCHEMA_FILE)
    learning_digest = _digest(learning_schema)
    learning_row = connection.execute(
        "SELECT schema_sha256 FROM problem_schema_migrations WHERE id = ?",
        (LEARNING_MIGRATION_ID,),
    ).fetchone()
    if learning_row is not None and (not has_learning or learning_row != (learning_digest,)):
        raise ValueError("이미 적용된 학습 경로 저장 SQL의 내용 또는 적용 이력이 변경되었습니다.")
    if tutor_row is not None and (not has_tutor or tutor_row != (tutor_digest,)):
        raise ValueError("이미 적용된 튜터 저장 SQL의 내용 또는 적용 이력이 변경되었습니다.")
    if type_row is not None and (not has_types or type_row != (type_digest,)):
        raise ValueError("이미 적용된 유형 저장 SQL의 내용 또는 적용 이력이 변경되었습니다.")
    if row is not None:
        if row != (digest,):
            raise ValueError("이미 적용된 006 SQL의 내용이 변경되었습니다.")
        _verify_objects(connection, full, current_columns)
    elif set(full).difference(base).intersection(_objects(connection)):
        raise ValueError("006 객체가 일부 존재합니다. 자동으로 덮어쓰지 않습니다.")
    if source_row is not None:
        if row is None or source_row != (source_digest,):
            raise ValueError("이미 적용된 007 SQL의 내용 또는 적용 이력이 변경되었습니다.")
        _verify_objects(connection, sources, current_columns)
    elif set(sources).difference(full).intersection(_objects(connection)):
        raise ValueError("007 객체가 일부 존재합니다. 자동으로 덮어쓰지 않습니다.")
    if (row is not None and source_row is not None and has_regions and type_row is not None
            and tutor_row is not None and learning_row is not None):
        _verify_objects(connection, extended, extended_columns)
        return "이미 적용됨: 풀이·OCR·유형·튜터·학습 경로 저장 구조 검증 완료"
    pending_schema = "" if row is not None else schema
    if not has_regions:
        pending_schema += "\n" + _schema(REGION_SCHEMA_FILE)
    if source_row is None:
        pending_schema += "\n" + source_schema
    if not has_types:
        pending_schema += "\n" + type_schema
    if not has_tutor:
        pending_schema += "\n" + tutor_schema
    if not has_learning:
        pending_schema += "\n" + learning_schema
    try:
        connection.executescript("BEGIN IMMEDIATE;\n" + pending_schema)
        _verify_objects(connection, extended, extended_columns)
        _has_region_schema(connection)
        _has_type_match_schema(connection)
        _has_strict_schema(connection, LEARNING_SCHEMA_FILE, "학습 경로")
        _verify_integrity(connection)
        if row is None:
            connection.execute(
                "INSERT INTO problem_schema_migrations(id, schema_sha256) VALUES (?, ?)",
                (MIGRATION_ID, digest),
            )
        if source_row is None:
            connection.execute(
                "INSERT INTO problem_schema_migrations(id, schema_sha256) VALUES (?, ?)",
                (SOURCE_MIGRATION_ID, source_digest),
            )
        if type_row is None:
            connection.execute(
                "INSERT INTO problem_schema_migrations(id, schema_sha256) VALUES (?, ?)",
                (TYPE_MIGRATION_ID, type_digest),
            )
        if tutor_row is None:
            connection.execute(
                "INSERT INTO problem_schema_migrations(id, schema_sha256) VALUES (?, ?)",
                (TUTOR_MIGRATION_ID, tutor_digest),
            )
        if learning_row is None:
            connection.execute(
                "INSERT INTO problem_schema_migrations(id, schema_sha256) VALUES (?, ?)",
                (LEARNING_MIGRATION_ID, learning_digest),
            )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    return "풀이·OCR·유형·튜터·학습 경로 저장 구조 적용 완료"


def _build_empty(path):
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for name, migration_id in BASE:
            connection.executescript("BEGIN IMMEDIATE;\n" + _schema(name))
            if migration_id:
                connection.execute(
                    "INSERT INTO problem_schema_migrations(id, schema_sha256) VALUES (?, ?)",
                    (migration_id, _digest(_schema(name))),
                )
            connection.commit()
        return apply_migration(connection)


def prepare(database, *, create_empty=False):
    """새 파일은 완성 후 등록하고, 기존 DB의 풀이·OCR·사진 출처·유형 구조를 준비한다."""
    database = Path(database).resolve()
    if database.exists():
        if not database.is_file():
            raise ValueError("DB 경로는 일반 파일이어야 합니다.")
        with closing(sqlite3.connect(database.as_uri() + "?mode=rw", uri=True)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            message = apply_migration(connection)
        return {"database": str(database), "action": "upgrade_or_verify", "message": message}
    if not create_empty:
        raise ValueError("새 개발 DB를 만들려면 --create-empty를 지정해 주세요.")
    database.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".solution-flow-", suffix=".sqlite3", dir=database.parent)
    os.close(descriptor)
    temporary = Path(temporary)
    try:
        _build_empty(temporary)
        # 경합 중 생성된 파일도 덮어쓰지 않는다.
        os.link(temporary, database)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "database": str(database), "action": "created_empty_development_database",
        "message": "문제은행 문항 없는 별도 개발 DB 생성 완료. 모델 실행 없음.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DATABASE)
    parser.add_argument("--create-empty", action="store_true", help="파일이 없을 때만 빈 개발 DB 생성")
    parser.add_argument("--with-auth", action="store_true", help="같은 개발 DB에 회원·세션 테이블도 준비")
    args = parser.parse_args()
    try:
        result = prepare(args.database, create_empty=args.create_empty)
        if args.with_auth:
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            from app.db.auth import metadata
            from app.db.connection import build_engine
            from sqlalchemy.engine import URL
            from sqlalchemy.exc import SQLAlchemyError

            engine = build_engine(URL.create("sqlite", database=result["database"]))
            try:
                metadata.create_all(engine)
            except SQLAlchemyError as error:
                raise ValueError("회원·세션 테이블을 준비하지 못했습니다.") from error
            finally:
                engine.dispose()
            result["auth_schema"] = "prepared"
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, sqlite3.Error) as error:
        parser.exit(1, f"DB 준비 실패: {error}\n")


if __name__ == "__main__":
    main()
