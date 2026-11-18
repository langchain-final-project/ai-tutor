"""기존 OCR 작업을 유지하면서 영역 저장 스키마를 준비한다."""

from pathlib import Path
import sqlite3


def ensure_ocr_region_schema(connection: sqlite3.Connection) -> None:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(problem_ocr_jobs)")}
    if not columns:
        raise ValueError("먼저 005 OCR 작업 마이그레이션을 적용해 주세요.")
    if "selected_region_json" not in columns:
        schema = (Path(__file__).resolve().parents[2] / "migrations/006_problem_ocr_regions.sqlite.sql").read_text(encoding="utf-8")
        try:
            connection.executescript("BEGIN IMMEDIATE;\n" + schema + "\nCOMMIT;")
        except BaseException:
            connection.rollback()
            raise

    trigger = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name = ?",
        ("trg_ocr_job_region_immutable",),
    ).fetchone()
    if trigger is None:
        raise ValueError("006 OCR 영역 마이그레이션 상태를 확인해 주세요.")
