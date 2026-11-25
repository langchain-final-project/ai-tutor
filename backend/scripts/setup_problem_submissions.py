"""개인 작업용 DB에 문제 제출·입력 버전 스키마를 적용한다."""

from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3

ROOT = Path(__file__).resolve().parents[2]
DATABASE = ROOT / "data/problem_bank/didgo.sqlite3"
SCHEMA = ROOT / "backend/migrations/004_problem_submissions.sqlite.sql"
MIGRATION_ID = "problem-submissions-v1"

EXPECTED = {
    "problem_submissions": "table",
    "problem_input_versions": "table",
    "trg_input_version_ocr_insert": "trigger",
    "trg_input_version_content_update": "trigger",
    "trg_submission_photo_update": "trigger",
    "trg_referenced_ocr_photo_update": "trigger",
    "trg_input_version_initial_state": "trigger",
    "trg_input_version_confirmation_update": "trigger",
}


def verify(connection):
    objects = dict(connection.execute(
        "SELECT name, type FROM sqlite_master"
    ))

    for name, kind in EXPECTED.items():
        if objects.get(name) != kind:
            raise ValueError(f"DB 객체 누락: {name}")

    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise ValueError("외래키 검사 실패")


def apply_migration(connection, schema):
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    )}

    required = {
        "uploaded_problems",
        "problem_ocr_results",
        "problem_schema_migrations",
    }

    if not required.issubset(tables):
        raise ValueError("기존 문제은행·OCR 테이블이 필요합니다.")

    checksum = hashlib.sha256(schema.encode("utf-8")).hexdigest()

    row = connection.execute(
        "SELECT schema_sha256 FROM problem_schema_migrations WHERE id = ?",
        (MIGRATION_ID,),
    ).fetchone()

    if row is not None:
        if row[0] != checksum:
            raise ValueError("이미 적용한 SQL의 내용이 변경되었습니다.")

        verify(connection)
        return "이미 적용됨: 구조 검증 완료"

    try:
        connection.executescript("BEGIN IMMEDIATE;\n" + schema)

        verify(connection)

        connection.execute(
            "INSERT INTO problem_schema_migrations (id, schema_sha256) "
            "VALUES (?, ?)",
            (MIGRATION_ID, checksum),
        )

        connection.commit()

    except BaseException:
        connection.rollback()
        raise

    return "적용 완료: 테이블 2개, 트리거 6개"


def main():
    schema = SCHEMA.read_text(encoding="utf-8")
    uri = DATABASE.resolve().as_uri() + "?mode=rw"

    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")

        print(apply_migration(connection, schema))
        print(f"대상 DB: {DATABASE}")


if __name__ == "__main__":
    main()