"""기존 문제은행에 사진 문제/OCR/유형 분류 저장 구조를 추가한다. 기존 코드는 수정하지 않는다."""

from __future__ import annotations

import argparse
from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys


ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "backend" / "migrations" / "002_problem_classification.sqlite.sql"
MIGRATION_ID = "problem-classification-v1"
BASE_SCRIPT = ROOT / "backend" / "scripts" / "build_problem_bank.py"
spec = importlib.util.spec_from_file_location("problem_bank_base", BASE_SCRIPT)
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
TABLES = (
    "problem_schema_migrations", "uploaded_problems", "problem_ocr_results",
    "problem_classifications", "problem_classification_candidates",
)
VIEWS = ("problem_topic_catalog", "question_classification_corpus", "classified_problems")
TRIGGERS = (
    "trg_classification_ocr_insert", "trg_classification_ocr_update", "trg_ocr_status_update",
    "trg_classification_topic_insert", "trg_classification_topic_update",
    "trg_candidate_topic_insert", "trg_candidate_topic_update", "trg_question_topic_update",
)


def schema_hash() -> str:
    # 줄바꿈 차이로 운영체제마다 체크섬이 바뀌지 않도록 텍스트로 읽는다.
    return base.digest(SCHEMA.read_text(encoding="utf-8").encode("utf-8"))


def verify_extension(connection: sqlite3.Connection) -> dict:
    for kind, names in (("table", TABLES), ("view", VIEWS), ("trigger", TRIGGERS)):
        actual = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = ?", (kind,))}
        base.require(set(names).issubset(actual), f"분류 DB {kind} 누락: {set(names) - actual}")
    row = connection.execute("SELECT schema_sha256 FROM problem_schema_migrations WHERE id = ?", (MIGRATION_ID,)).fetchone()
    base.require(row is not None and row[0] == schema_hash(), "분류 DB 마이그레이션 해시 불일치")
    base.require(connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)], "DB 무결성 검사 실패")
    base.require(not connection.execute("PRAGMA foreign_key_check").fetchall(), "외래키 검사 실패")
    topic_count = connection.execute("SELECT COUNT(DISTINCT source_topic_code) FROM problem_topic_catalog").fetchone()[0]
    base.require(topic_count == 1165, "분류 유형 1,165개 불일치")
    corpus_count = connection.execute("SELECT COUNT(*) FROM question_classification_corpus").fetchone()[0]
    base.require(corpus_count == 30339, "분류용 문항 30,339개 불일치")
    invalid = connection.execute("""
        SELECT COUNT(*) FROM problem_classifications c
        JOIN problem_ocr_results o ON o.id = c.ocr_result_id
        LEFT JOIN questions q ON q.id = c.matched_question_id
        WHERE o.status <> 'completed'
           OR (c.matched_question_id IS NOT NULL AND q.source_topic_code <> c.source_topic_code)
    """).fetchone()[0]
    invalid_candidates = connection.execute("""
        SELECT COUNT(*) FROM problem_classification_candidates c
        JOIN questions q ON q.id = c.matched_question_id
        WHERE q.source_topic_code <> c.source_topic_code
    """).fetchone()[0]
    base.require(invalid == 0 and invalid_candidates == 0, "분류 결과 연결 불일치")
    return {
        "classification_schema": MIGRATION_ID,
        "classification_topics": topic_count,
        "classification_corpus": corpus_count,
        **{table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in TABLES[1:]},
    }


def apply_extension(connection: sqlite3.Connection) -> dict:
    existing = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
    if "problem_schema_migrations" in existing:
        # 재실행해도 기존 업로드/분류 데이터를 덮어쓰거나 삭제하지 않는다.
        return verify_extension(connection)
    base.require(not (set(TABLES + VIEWS + TRIGGERS) & existing), "이미 존재하는 분류 DB 객체와 충돌")
    try:
        connection.executescript("BEGIN IMMEDIATE;\n" + SCHEMA.read_text(encoding="utf-8"))
        connection.execute("INSERT INTO problem_schema_migrations (id, schema_sha256) VALUES (?, ?)",
                           (MIGRATION_ID, schema_hash()))
        report = verify_extension(connection)
        connection.commit()
        return report
    except BaseException:
        connection.rollback()
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=base.DEFAULT_DB)
    parser.add_argument("--source-dir", type=Path, default=base.SOURCE_DIR)
    parser.add_argument("--verify", action="store_true", help="DB를 읽기 전용으로 검증한다")
    args = parser.parse_args()
    try:
        data = base.load_sources(args.source_dir)
        if not args.database.exists() and not args.verify:
            base.build(args.database, data)
        uri = args.database.resolve().as_uri() + ("?mode=ro" if args.verify else "?mode=rw")
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            report = base.verify(connection, data)
            extension = verify_extension(connection) if args.verify else apply_extension(connection)
        print(json.dumps({"database": str(args.database.resolve()), **report, **extension}, ensure_ascii=False, indent=2))
    except (OSError, ValueError, sqlite3.Error, KeyError) as error:
        print(f"문제 분류 DB 작업 실패: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
