"""개인 작업용 DB에 OCR 작업·스트리밍 이벤트 스키마를 적용한다."""

from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3

ROOT = Path(__file__).resolve().parents[2]
DATABASE = ROOT / "data/problem_bank/didgo.sqlite3"
SCHEMA = ROOT / "backend/migrations/005_problem_ocr_jobs.sqlite.sql"
MIGRATION_ID = "problem-ocr-jobs-v1"

EXPECTED = {
    "problem_ocr_jobs": "table",
    "problem_ocr_job_events": "table",
    "ux_problem_ocr_jobs_active": "index",
    "ix_problem_ocr_job_events_job": "index",
    "trg_ocr_job_submission_insert": "trigger",
    "trg_ocr_job_result_update": "trigger",
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
        "problem_schema_migrations",
        "problem_submissions",
        "problem_input_versions",
        "problem_ocr_results",
    }

    if not required.issubset(tables):
        raise ValueError("먼저 문제 제출·OCR 저장 구조를 준비해 주세요.")

    previous = connection.execute(
        "SELECT id FROM problem_schema_migrations WHERE id = ?",
        ("problem-submissions-v1",),
    ).fetchone()

    if previous is None:
        raise ValueError("먼저 004 마이그레이션을 적용해 주세요.")

    checksum = hashlib.sha256(schema.encode("utf-8")).hexdigest()

    row = connection.execute(
        "SELECT schema_sha256 FROM problem_schema_migrations WHERE id = ?",
        (MIGRATION_ID,),
    ).fetchone()

    if row is not None:
        if row[0] != checksum:
            raise ValueError("이미 적용한 SQL의 내용이 변경되었습니다.")

        verify(connection)
        return "이미 적용됨: OCR 작업 구조 검증 완료"

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

    return "적용 완료: 테이블 2개, 인덱스 2개, 트리거 2개"


def main():
    schema = SCHEMA.read_text(encoding="utf-8")
    uri = DATABASE.resolve().as_uri() + "?mode=rw"

    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")

        print(apply_migration(connection, schema))
        print(f"대상 DB: {DATABASE}")


if __name__ == "__main__":
    main()