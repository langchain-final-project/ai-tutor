"""원본 ZIP에서 딛고 SQLite 문항 DB 생성/검증. Python 표준 라이브러리만 사용한다."""

from __future__ import annotations

import argparse
from contextlib import closing
import csv
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import sqlite3
import sys
import zipfile


ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIR = ROOT / "data" / "problem_bank" / "sources"
SCHEMA = ROOT / "backend" / "migrations" / "001_problem_bank.sqlite.sql"
DEFAULT_DB = ROOT / "data" / "problem_bank" / "didgo.sqlite3"
FIELDS = (
    "id", "grade", "semester", "unit_code", "unit_name", "source_id",
    "subquestion_number", "source_topic_code", "source_topic_name", "question_text",
    "equation_latex", "answer", "target_skill", "skill_tags", "question_type",
    "source_difficulty", "question_filename", "answer_filename", "figure_text",
    "answer_text", "answer_box_texts", "review_status",
)
ARRAY_FIELDS = {"skill_tags", "answer_box_texts"}
CSV_FIELDS = (
    "학기", "대표 인식 유형", "단원", "세부 유형", "선정 이유", "원본 문제 파일명",
    "모범답안 파일명", "JSON 파일명", "원본 문제 경로", "모범답안 경로", "JSON 경로",
    "출처 문제은행 JSON",
)
BENCHMARK_ID = "ocr50-v1"


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def checked_archive(path: Path, expected: dict) -> zipfile.ZipFile:
    with path.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    require(actual == expected["sha256"], f"원본 ZIP 체크섬 불일치: {path.name}")
    require(path.stat().st_size == expected["bytes"], f"ZIP 크기 불일치: {path.name}")
    archive = zipfile.ZipFile(path)
    if len(archive.namelist()) != len(set(archive.namelist())):
        archive.close()
        raise ValueError(f"중복 ZIP 경로: {path.name}")
    return archive


def load_sources(source_dir: Path) -> dict:
    manifest = json.loads((source_dir / "manifest.json").read_text(encoding="utf-8"))
    questions, members, units, topics = {}, {}, {}, {}
    archives = manifest["archives"]
    with checked_archive(source_dir / "problem_bank.zip", archives["problem_bank"]) as bank:
        json_members = sorted(n for n in bank.namelist() if n.endswith(".json"))
        require(len(json_members) == 24, "문제은행 단원 JSON은 24개여야 합니다.")
        for member in json_members:
            rows = json.loads(bank.read(member).decode("utf-8-sig"))
            require(isinstance(rows, list), f"문항 배열 아님: {member}")
            for row in rows:
                require(set(row) == set(FIELDS), f"원본 22개 필드 불일치: {member}")
                question_id = row["id"]
                require(question_id not in questions, f"중복 문항 ID: {question_id}")
                for field in ARRAY_FIELDS:
                    require(isinstance(row[field], list) and all(isinstance(v, str) for v in row[field]),
                            f"문자열 배열 아님: {question_id}/{field}")
                key = (row["grade"], row["semester"], row["unit_code"])
                require(units.setdefault(key, row["unit_name"]) == row["unit_name"],
                        f"같은 단원 코드의 이름 충돌: {key}")
                require(topics.setdefault(row["source_topic_code"], row["source_topic_name"])
                        == row["source_topic_name"], f"유형 이름 충돌: {question_id}")
                questions[question_id] = row
                members[question_id] = member
    require(len(questions) == 30339, "전체 문제은행은 30,339문항이어야 합니다.")
    assets, items = {}, []
    with checked_archive(source_dir / "ocr50.zip", archives["ocr50"]) as bench:
        names = set(bench.namelist())
        csv_names = [n for n in names if n.endswith(".csv")]
        require(len(csv_names) == 1, "선정목록 CSV는 1개여야 합니다.")
        root = str(PurePosixPath(csv_names[0]).parent)
        reader = csv.DictReader(io.StringIO(bench.read(csv_names[0]).decode("utf-8-sig"), newline=""))
        require(reader.fieldnames == list(CSV_FIELDS), "선정목록 CSV 열 불일치")
        selected, selected_json = set(), set()
        for position, meta in enumerate(reader, 1):
            question_id = meta["원본 문제 파일명"]
            require(question_id in questions and question_id not in selected,
                    f"평가 문항 누락 또는 중복: {question_id}")
            selected.add(question_id)
            json_member = f"{root}/{meta['JSON 경로']}"
            require(json_member in names, f"평가 JSON 누락: {json_member}")
            selected_json.add(json_member)
            snapshot = json.loads(bench.read(json_member).decode("utf-8-sig"))
            require(snapshot == questions[question_id], f"평가 문항과 문제은행 원본 불일치: {question_id}")
            require(meta["출처 문제은행 JSON"] == PurePosixPath(members[question_id]).name,
                    f"출처 문제은행 파일 불일치: {question_id}")
            require(meta["모범답안 파일명"] == snapshot["answer_filename"], f"답안 파일명 불일치: {question_id}")
            require(meta["단원"] == snapshot["unit_name"] and meta["학기"] == f"{snapshot['semester']}학기",
                    f"학기/단원 불일치: {question_id}")
            image_members = []
            for path_key, filename_key in (("원본 문제 경로", "원본 문제 파일명"), ("모범답안 경로", "모범답안 파일명")):
                member = f"{root}/{meta[path_key]}"
                require(member in names and PurePosixPath(member).name == meta[filename_key],
                        f"평가 이미지 누락/파일명 불일치: {member}")
                content = bench.read(member)
                require(content.startswith(b"\x89PNG\r\n\x1a\n"), f"PNG 이미지 아님: {member}")
                assets[member] = {"filename": meta[filename_key], "sha256": digest(content), "content": content}
                image_members.append(member)
            items.append({"position": position, "question_id": question_id,
                          "question_asset": image_members[0], "answer_asset": image_members[1],
                          "metadata": meta})
        require(selected_json == {n for n in names if n.endswith('.json')}, "평가 JSON 목록 불일치")
        require(set(assets) == {n for n in names if n.endswith('.png')}, "평가 이미지 목록 불일치")
    require(len(items) == 50 and len(assets) == 100, "평가는 50문항/100이미지여야 합니다.")
    fingerprint_rows = [
        {**item, "question_sha256": digest(canonical(questions[item["question_id"]]).encode("utf-8")),
         "question_image_sha256": assets[item["question_asset"]]["sha256"],
         "answer_image_sha256": assets[item["answer_asset"]]["sha256"]}
        for item in items
    ]
    return {"manifest": manifest, "questions": questions, "members": members, "units": units,
            "topics": topics, "assets": assets, "items": items,
            "fingerprint": digest(canonical(fingerprint_rows).encode("utf-8"))}


def insert_data(connection: sqlite3.Connection, data: dict) -> None:
    connection.executemany("INSERT INTO dataset_sources VALUES (?, ?, ?)",
                           [(name, info["sha256"], info["bytes"])
                            for name, info in sorted(data["manifest"]["archives"].items())])
    connection.executemany("INSERT INTO units VALUES (?, ?, ?, ?)",
                           [(*key, name) for key, name in sorted(data["units"].items())])
    connection.executemany("INSERT INTO topics VALUES (?, ?)", sorted(data["topics"].items()))
    sql = f"INSERT INTO questions ({', '.join(FIELDS)}, source_member, content_sha256) VALUES ({', '.join(['?'] * 24)})"
    connection.executemany(sql, [
        (*[canonical(row[f]) if f in ARRAY_FIELDS else row[f] for f in FIELDS],
         data["members"][qid], digest(canonical(row).encode("utf-8")))
        for qid, row in sorted(data["questions"].items())
    ])
    connection.executemany("INSERT INTO benchmark_assets VALUES (?, ?, ?, ?, ?)",
                           [(member, asset["filename"], "image/png", asset["sha256"], asset["content"])
                            for member, asset in sorted(data["assets"].items())])
    connection.execute("INSERT INTO benchmark_sets VALUES (?, ?, ?, ?)",
                       (BENCHMARK_ID, len(data["items"]), data["fingerprint"], "ocr50"))
    connection.executemany("INSERT INTO benchmark_items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", [
        (BENCHMARK_ID, item["position"], item["question_id"], item["metadata"]["대표 인식 유형"],
         item["metadata"]["선정 이유"], item["metadata"]["출처 문제은행 JSON"],
         item["question_asset"], item["answer_asset"], canonical(item["metadata"]))
        for item in data["items"]
    ])


def verify(connection: sqlite3.Connection, data: dict) -> dict:
    require(connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)], "SQLite 무결성 검사 실패")
    require(not connection.execute("PRAGMA foreign_key_check").fetchall(), "외래키 검사 실패")
    require(connection.execute("PRAGMA user_version").fetchone()[0] == 1, "스키마 버전 불일치")
    cursor = connection.execute(f"SELECT {', '.join(FIELDS)}, source_member, content_sha256 FROM questions ORDER BY id")
    count = 0
    for values in cursor:
        row = dict(zip(FIELDS, values[:22]))
        for field in ARRAY_FIELDS:
            row[field] = json.loads(row[field])
        require(row == data["questions"].get(row["id"]), f"적재 문항 원본 불일치: {row['id']}")
        require(values[22] == data["members"][row["id"]], f"문항 출처 불일치: {row['id']}")
        require(values[23] == digest(canonical(row).encode("utf-8")), f"문항 해시 불일치: {row['id']}")
        count += 1
    require(count == len(data["questions"]), "적재 문항 수 불일치")
    actual_sources = connection.execute("SELECT name, archive_sha256, archive_bytes FROM dataset_sources ORDER BY name").fetchall()
    expected_sources = [(name, info["sha256"], info["bytes"]) for name, info in sorted(data["manifest"]["archives"].items())]
    require(actual_sources == expected_sources, "원본 ZIP 이력 불일치")
    require(connection.execute("SELECT * FROM units ORDER BY grade, semester, unit_code").fetchall()
            == [(*key, name) for key, name in sorted(data["units"].items())], "단원 불일치")
    require(connection.execute("SELECT * FROM topics ORDER BY source_topic_code").fetchall()
            == sorted(data["topics"].items()), "유형 불일치")
    actual_assets = connection.execute("SELECT * FROM benchmark_assets ORDER BY source_member").fetchall()
    require(actual_assets == [(member, asset["filename"], "image/png", asset["sha256"], asset["content"])
                             for member, asset in sorted(data["assets"].items())], "이미지 바이트/해시 불일치")
    actual_items = connection.execute("SELECT * FROM benchmark_items ORDER BY position").fetchall()
    expected_items = [(BENCHMARK_ID, item["position"], item["question_id"],
                       item["metadata"]["대표 인식 유형"], item["metadata"]["선정 이유"],
                       item["metadata"]["출처 문제은행 JSON"], item["question_asset"],
                       item["answer_asset"], canonical(item["metadata"])) for item in data["items"]]
    require(actual_items == expected_items, "평가 순서/선정목록 불일치")
    require(connection.execute("SELECT * FROM benchmark_sets").fetchall()
            == [(BENCHMARK_ID, 50, data["fingerprint"], "ocr50")], "평가 데이터 버전/해시 불일치")
    require(connection.execute("SELECT COUNT(*) FROM training_questions").fetchone()[0] == 30289,
            "평가 문항 제외 목록 불일치")
    return {"questions": count, "units": len(data["units"]), "topics": len(data["topics"]),
            "by_grade": dict(connection.execute("SELECT grade, COUNT(*) FROM questions GROUP BY grade")),
            "null_answers": connection.execute("SELECT COUNT(*) FROM questions WHERE answer IS NULL").fetchone()[0],
            "benchmark": BENCHMARK_ID, "benchmark_items": len(actual_items),
            "benchmark_images": len(actual_assets), "benchmark_sha256": data["fingerprint"],
            "training_questions": 30289, "integrity": "ok", "foreign_keys": "ok"}


def build(database: Path, data: dict) -> dict:
    database.parent.mkdir(parents=True, exist_ok=True)
    # 배타적으로 생성해 기존 DB는 내용/크기에 관계없이 덮어쓰지 않는다.
    with database.open("xb"):
        pass
    connection = None
    try:
        connection = sqlite3.connect(database)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript("BEGIN IMMEDIATE;\n" + SCHEMA.read_text(encoding="utf-8"))
        insert_data(connection, data)
        report = verify(connection, data)
        connection.commit()
    except BaseException:
        if connection is not None:
            connection.rollback()
            connection.close()
        database.unlink(missing_ok=True)
        raise
    else:
        connection.close()
        return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DB, help="생성하거나 검증할 SQLite 경로")
    parser.add_argument("--source-dir", type=Path, default=SOURCE_DIR, help="ZIP 2개와 manifest.json이 있는 폴더")
    parser.add_argument("--verify", action="store_true", help="기존 DB를 읽기 전용으로 전체 원본과 대조")
    args = parser.parse_args()
    try:
        if not args.verify and args.database.exists():
            raise FileExistsError(f"기존 DB를 덮어쓰지 않습니다. 다른 --database 경로를 지정하세요: {args.database}")
        data = load_sources(args.source_dir)
        if args.verify:
            uri = args.database.resolve().as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True)) as connection:
                report = verify(connection, data)
        else:
            report = build(args.database, data)
        print(json.dumps({"database": str(args.database.resolve()), **report}, ensure_ascii=False, indent=2))
    except (OSError, ValueError, sqlite3.Error, zipfile.BadZipFile, KeyError) as error:
        print(f"DB 작업 실패: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
