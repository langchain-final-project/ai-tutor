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


def _schema(name):
    return (MIGRATIONS / name).read_text(encoding="utf-8")


def _digest(schema):
    return hashlib.sha256(schema.encode("utf-8")).hexdigest()


def _objects(connection):
    return dict(connection.execute(
        "SELECT name, type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
    ))


def _reference():
    with closing(sqlite3.connect(":memory:")) as reference:
        for name, _ in BASE:
            reference.executescript(_schema(name))
        base = _objects(reference)
        reference.executescript(_schema(SCHEMA_FILE))
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


def apply_migration(connection):
    """기존 데이터는 유지하고 006을 원자적으로 적용한다."""
    if connection.in_transaction:
        raise ValueError("진행 중인 저장 작업을 마친 뒤 마이그레이션해 주세요.")
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise ValueError("외래키 검사를 활성화해 주세요.")
    base, full, columns = _reference()
    _verify_objects(connection, base, columns)
    _verify_history(connection)
    _verify_integrity(connection)
    schema = _schema(SCHEMA_FILE)
    digest = _digest(schema)
    row = connection.execute(
        "SELECT schema_sha256 FROM problem_schema_migrations WHERE id = ?",
        (MIGRATION_ID,),
    ).fetchone()
    if row is not None:
        if row != (digest,):
            raise ValueError("이미 적용된 006 SQL의 내용이 변경되었습니다.")
        _verify_objects(connection, full, columns)
        return "이미 적용됨: 저장 구조 검증 완료"
    if set(full).difference(base).intersection(_objects(connection)):
        raise ValueError("006 객체가 일부 존재합니다. 자동으로 덮어쓰지 않습니다.")
    try:
        connection.executescript("BEGIN IMMEDIATE;\n" + schema)
        _verify_objects(connection, full, columns)
        _verify_integrity(connection)
        connection.execute(
            "INSERT INTO problem_schema_migrations(id, schema_sha256) VALUES (?, ?)",
            (MIGRATION_ID, digest),
        )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    return "006 적용 완료"


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
    """새 파일은 완성 후 등록하고, 기존 DB에는 006만 적용한다."""
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
