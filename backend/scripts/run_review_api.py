from contextlib import closing
import os
from pathlib import Path
import sqlite3
import sys

import uvicorn

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.submission_api import create_submission_app
from app.db.ocr_regions import ensure_ocr_region_schema

DATABASE = ROOT / "data/review-preview.sqlite3"

MIGRATIONS = (
    "001_problem_bank.sqlite.sql",
    "002_problem_classification.sqlite.sql",
    "004_problem_submissions.sqlite.sql",
    "005_problem_ocr_jobs.sqlite.sql",
)


def prepare_database():
    DATABASE.parent.mkdir(parents=True, exist_ok=True)

    with closing(sqlite3.connect(DATABASE)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")

        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }

        if not tables:
            schema = "\n".join(
                (ROOT / "backend/migrations" / name).read_text(
                    encoding="utf-8"
                )
                for name in MIGRATIONS
            )

            connection.executescript(
                "BEGIN IMMEDIATE;\n" + schema + "\nCOMMIT;"
            )

        required = {
            "uploaded_problems",
            "problem_ocr_results",
            "problem_submissions",
            "problem_input_versions",
            "problem_ocr_jobs",
            "problem_ocr_job_events",
        }

        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

        if not required.issubset(tables):
            raise RuntimeError("화면 테스트용 DB 구조를 확인해 주세요.")

        ensure_ocr_region_schema(connection)


if __name__ == "__main__":
    os.environ.setdefault(
        "DIDGO_QWEN_URL", "http://127.0.0.1:8081/v1"
    )
    os.environ.setdefault("DIDGO_OCR_TIMEOUT", "300")

    prepare_database()

    app = create_submission_app(
        DATABASE,
        dev_token="local-test-token",
        owner_id="local-student",
    )

    print(f"화면 테스트 DB: {DATABASE}")

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8001,
        workers=1,
    )
