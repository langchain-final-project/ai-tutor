"""개인 작업용 DB에 OCR 선택 영역 저장 구조를 추가한다."""

import argparse
from contextlib import closing
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.db.ocr_regions import ensure_ocr_region_schema


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=ROOT / "data/problem_bank/didgo.sqlite3")
    args = parser.parse_args()
    uri = args.database.resolve().as_uri() + "?mode=rw"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        ensure_ocr_region_schema(connection)
    print(f"OCR 영역 저장 구조 준비 완료: {args.database.resolve()}")
