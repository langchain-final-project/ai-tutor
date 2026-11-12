"""pull 받은 완성 DB를 검증하고 개인 작업용 DB로 복사한다. 재적재/외부 패키지는 필요 없다."""

from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys


# Docker에서는 이 파일만 /tools에 마운트해서 실행할 수도 있다.
ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = ROOT / "data" / "problem_bank"
TABLES = (
    "questions", "units", "topics", "benchmark_sets", "benchmark_items", "benchmark_assets",
    "uploaded_problems", "problem_ocr_results", "problem_classifications", "problem_classification_candidates",
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def read_connection(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def inspect_database(path: Path, manifest: dict, *, seed: bool) -> dict:
    with closing(read_connection(path)) as connection:
        require(connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)], "DB 무결성 검사 실패")
        require(not connection.execute("PRAGMA foreign_key_check").fetchall(), "DB 외래키 검사 실패")
        counts = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in TABLES}
        for table, expected in manifest["counts"].items():
            # 개인 작업 DB의 업로드/OCR/분류 이력은 이미 존재할 수 있다.
            if seed or table not in manifest["operational_tables"]:
                require(counts[table] == expected, f"DB 문항/테이블 수 불일치: {table}")
        benchmark = connection.execute("SELECT id, content_sha256 FROM benchmark_sets").fetchall()
        require(benchmark == [(manifest["benchmark_id"], manifest["benchmark_sha256"])], "평가 세트 버전 불일치")
        migration = connection.execute(
            "SELECT schema_sha256 FROM problem_schema_migrations WHERE id = ?",
            (manifest["classification_schema"],),
        ).fetchone()
        require(migration == (manifest["classification_schema_sha256"],), "문제 분류 스키마 버전 불일치")
        return counts


def verify_seed(seed_path: Path, manifest: dict) -> None:
    require(seed_path.stat().st_size == manifest["bytes"], "공유 DB 파일 크기 불일치")
    with seed_path.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    require(actual == manifest["sha256"], "공유 DB SHA-256 불일치")
    inspect_database(seed_path, manifest, seed=True)


def prepare(seed_path: Path, database: Path, manifest: dict) -> dict:
    require(seed_path.resolve() != database.resolve(), "공유 원본을 작업용 DB로 지정할 수 없습니다.")
    verify_seed(seed_path, manifest)
    if database.exists():
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=rw", uri=True)) as connection:
            objects = connection.execute("SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
            if not objects:
                # 이전 health 확인만 실행한 빈 SQLite 볼륨도 자동 초기화한다.
                with closing(read_connection(seed_path)) as source:
                    source.backup(connection)
                return {"action": "initialized_empty_database", **inspect_database(database, manifest, seed=True)}
        return {"action": "kept_existing_database", **inspect_database(database, manifest, seed=False)}
    database.parent.mkdir(parents=True, exist_ok=True)
    # 기존 파일을 덮어쓰지 않는다. 복사 실패 시 이번 실행에서 만든 파일만 제거한다.
    with database.open("xb") as destination:
        try:
            with seed_path.open("rb") as source:
                shutil.copyfileobj(source, destination)
        except BaseException:
            destination.close()
            database.unlink(missing_ok=True)
            raise
    try:
        counts = inspect_database(database, manifest, seed=True)
    except BaseException:
        database.unlink(missing_ok=True)
        raise
    return {"action": "copied_shared_database", **counts}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=Path, default=DATA_DIR / "problem_bank.seed.sqlite3")
    parser.add_argument("--manifest", type=Path, default=DATA_DIR / "problem_bank.seed.json")
    parser.add_argument("--database", type=Path, default=DATA_DIR / "didgo.sqlite3")
    parser.add_argument("--verify-seed", action="store_true", help="공유 원본을 읽기 전용 검증하고 복사하지 않는다")
    args = parser.parse_args()
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        require(manifest["format_version"] == 1, "지원하지 않는 공유 DB manifest 버전")
        if args.verify_seed:
            verify_seed(args.seed, manifest)
            report = {"action": "verified_shared_database"}
        else:
            report = prepare(args.seed, args.database, manifest)
        print(json.dumps({
            **report,
            "seed": str(args.seed.resolve()),
            "database": str(args.database.resolve()),
            "database_url": "sqlite:///" + args.database.resolve().as_posix(),
            "benchmark_sha256": manifest["benchmark_sha256"],
        }, ensure_ascii=False, indent=2))
    except (OSError, ValueError, sqlite3.Error, KeyError) as error:
        print(f"공유 DB 준비 실패: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
