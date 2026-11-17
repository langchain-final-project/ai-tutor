"""원본 ZIP과 문제은행 연결, 평가용 분리, 영역 좌표 및 추가 메타데이터 준비."""

import argparse
from contextlib import ExitStack, closing
import hashlib
import html
import json
from pathlib import Path, PurePosixPath
import re
import sqlite3
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
from app.db.raw_problem_data import (  # noqa: E402
    DEFAULT_DATASET_ROOT, DEFAULT_RAW_DB, RawProblemStore,
    archive_path, png_dimensions, read_connection, require,
)

SCHEMA = ROOT / "backend/migrations/003_raw_problem_sources.sqlite.sql"
DEFAULT_BANK = ROOT / "data/problem_bank/didgo.sqlite3"
if not DEFAULT_BANK.exists():
    DEFAULT_BANK = ROOT / "data/problem_bank/problem_bank.seed.sqlite3"


def packed(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def normalized_body(text: str) -> str:
    text = html.unescape(text).replace("−", "-").replace("‒", "-")
    text = re.sub(r"</?(?:table|tbody|thead|tr|td|th|caption|br|p|span)\b[^>]*>", " ", text, flags=re.I)
    return re.sub(r"\s+", "", text).replace("$", "")


def input_fingerprints(body: str, figure: str | None) -> tuple[str, str, str]:
    clean = normalized_body(body)
    # 구분자를 두어 본문/그림 설명 경계가 사라지지 않도록 한다.
    full_input = packed([clean, normalized_body(figure or "")])
    template = re.sub(r"\d+(?:\.\d+)?", "NUM", clean)
    return tuple(sha256(text.encode("utf-8")) for text in (clean, full_input, template))


def member_map(archive: zipfile.ZipFile, suffix: str) -> dict[str, str]:
    result = {}
    for info in archive.infolist():
        if info.is_dir() or not info.filename.lower().endswith(suffix):
            continue
        filename = PurePosixPath(info.filename.replace("\\", "/")).name
        require(filename not in result, f"ZIP 내부 파일명 중복: {filename}")
        result[filename] = info.filename
    return result


def load_labels(archive: zipfile.ZipFile) -> dict[str, dict]:
    result = {}
    for member in member_map(archive, ".json").values():
        row = json.loads(archive.read(member).decode("utf-8-sig"))
        question_id = row["question_filename"]
        require(question_id not in result, f"라벨 문항 중복: {question_id}")
        result[question_id] = row
    return result


def discover_archives(dataset_root: Path, grades: list[int]) -> dict:
    paths = list(dataset_root.rglob("*.zip"))
    result = {}
    for split, source_prefix, label_prefix in (("Training", "TS", "TL"), ("Validation", "VS", "VL")):
        for grade in grades:
            for role, prefix in (
                ("question_image", source_prefix + "_1."),
                ("answer_image", source_prefix + "_2."),
                ("question_label", label_prefix + "_1."),
                ("answer_label", label_prefix + "_2."),
            ):
                matches = [p for p in paths if p.name.startswith(prefix) and p.name.endswith(f"_{grade}학년.zip")]
                require(len(matches) == 1, f"{split}/중{grade}/{role}: ZIP이 정확히 1개 필요합니다.")
                result[(split, grade, role)] = matches[0]
    return result


def insert_sources(connection: sqlite3.Connection, dataset_root: Path, grades: list[int], bank_path: Path) -> None:
    with closing(read_connection(bank_path)) as bank:
        bank_rows = {r["id"]: dict(r) for r in bank.execute(
            "SELECT id, grade, question_text, figure_text, source_topic_code, answer_filename FROM questions"
        ) if r["grade"] in grades}
        reserved = {r[0] for r in bank.execute("SELECT question_id FROM benchmark_items")}
    archives = discover_archives(dataset_root, grades)
    with ExitStack() as stack:
        opened = {}
        for (split, grade, role), path in archives.items():
            archive_key = f"{split}:{grade}:{role}"
            connection.execute("INSERT INTO source_archives VALUES (?, ?, ?, ?, ?, ?, ?)", (
                archive_key, split, grade, role, path.relative_to(dataset_root).as_posix(),
                path.stat().st_size, file_sha256(path),
            ))
            opened[(split, grade, role)] = stack.enter_context(zipfile.ZipFile(path))
        for split in ("Training", "Validation"):
            for grade in grades:
                questions = load_labels(opened[(split, grade, "question_label")])
                answers = load_labels(opened[(split, grade, "answer_label")])
                require(set(questions) == set(answers), f"{split}/중{grade}: 문제/답안 라벨 목록 불일치")
                image_members = {kind: member_map(opened[(split, grade, kind + "_image")], ".png")
                                 for kind in ("question", "answer")}
                used = {"question": set(), "answer": set()}
                for question_id in sorted(questions):
                    q, a = questions[question_id], answers[question_id]
                    require(len(q["question_info"]) == 1, f"단일 메타데이터가 필요합니다: {question_id}")
                    info = q["question_info"][0]
                    require(info["question_grade"] == f"M{grade}", f"학년 불일치: {question_id}")
                    body = "\n".join(item["question_text"] for item in q["OCR_info"])
                    figures = [item.get("figure_text") for item in q["OCR_info"] if item.get("figure_text")]
                    figure = "\n".join(figures) if figures else None
                    answer_text = "\n".join(item["answer_text"] for item in a["answer_info"])
                    answer_names = {item["answer_filename"] for item in a["answer_info"]}
                    require(len(answer_names) == 1, f"답안 이미지가 여러 개입니다: {question_id}")
                    answer_filename = next(iter(answer_names))
                    linked = bank_rows.get(question_id)
                    if split == "Training":
                        require(linked is not None, f"기존 문제은행에서 찾을 수 없습니다: {question_id}")
                        require((body, figure, info["question_topic"], answer_filename) == (
                            linked["question_text"], linked["figure_text"], linked["source_topic_code"], linked["answer_filename"]
                        ), f"원천 문제/문제은행 불일치: {question_id}")
                    else:
                        require(linked is None, f"Validation 문항 ID가 문제은행과 겹칩니다: {question_id}")
                    values = (
                        split, question_id, question_id if linked else None, grade,
                        info["question_term"], str(info["question_unit"]).zfill(2),
                        info["question_topic"], info["question_topic_name"], info["question_type1"],
                        info["question_difficulty"], info.get("question_sector1"), info.get("question_sector2"),
                        info.get("question_step"), body, figure, answer_text,
                        packed(info), packed(q), packed(a), int(question_id in reserved),
                        *input_fingerprints(body, figure),
                    )
                    connection.execute(f"INSERT INTO raw_problems VALUES ({','.join(['?'] * len(values))})", values)
                    for kind, filename in (("question", question_id), ("answer", answer_filename)):
                        require(filename in image_members[kind], f"원천 이미지 누락: {filename}")
                        used[kind].add(filename)
                        member = image_members[kind][filename]
                        content = opened[(split, grade, kind + "_image")].read(member)
                        width, height = png_dimensions(content)
                        connection.execute("INSERT INTO source_assets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
                            split, question_id, kind, f"{split}:{grade}:{kind}_image", member, filename,
                            len(content), sha256(content), width, height,
                        ))
                for kind in used:
                    require(used[kind] == set(image_members[kind]), f"연결되지 않은 이미지가 있습니다: {split}/중{grade}/{kind}")
                print(f"연결 완료: {split} 중{grade} {len(questions):,}문항 / 이미지 {len(questions)*2:,}개", flush=True)
        train_ids = {r[0] for r in connection.execute("SELECT question_id FROM raw_problems WHERE split='Training'")}
        require(train_ids == set(bank_rows), "문제은행 문항과 Training 전체 목록이 일치하지 않습니다.")


def summarize(connection: sqlite3.Connection) -> dict:
    def scalar(sql):
        return connection.execute(sql).fetchone()[0]
    unknown = [r[0] for r in connection.execute(
        """SELECT DISTINCT v.source_topic_code FROM raw_problems v WHERE v.split='Validation'
           AND NOT EXISTS (SELECT 1 FROM classifier_training_pool t WHERE t.source_topic_code=v.source_topic_code)
           ORDER BY v.source_topic_code"""
    )]
    regions = {}
    for kind, view in (("question", "question_regions"), ("answer", "answer_regions")):
        regions[kind] = {r[0]: r[1] for r in connection.execute(f"SELECT region_type,COUNT(*) FROM {view} GROUP BY region_type")}
    return {
        "questions": {r[0]: r[1] for r in connection.execute("SELECT split,COUNT(*) FROM raw_problems GROUP BY split")},
        "images": scalar("SELECT COUNT(*) FROM source_assets"),
        "bank_links": scalar("SELECT COUNT(*) FROM raw_problems WHERE bank_question_id IS NOT NULL"),
        "reserved_ocr50": scalar("SELECT COUNT(*) FROM raw_problems WHERE reserved_ocr50=1"),
        "classifier_training_pool": scalar("SELECT COUNT(*) FROM classifier_training_pool"),
        "validation_unseen_topic_codes": unknown,
        "validation_same_body_as_training": scalar("""SELECT COUNT(*) FROM raw_problems v WHERE v.split='Validation'
            AND EXISTS (SELECT 1 FROM raw_problems t WHERE t.split='Training' AND t.body_sha256=v.body_sha256)"""),
        "validation_same_input_as_training": scalar("""SELECT COUNT(*) FROM raw_problems v WHERE v.split='Validation'
            AND EXISTS (SELECT 1 FROM raw_problems t WHERE t.split='Training' AND t.input_sha256=v.input_sha256)"""),
        "regions": regions,
        "sector1": {r[0]: r[1] for r in connection.execute("SELECT sector1,COUNT(*) FROM raw_problems WHERE split='Training' GROUP BY sector1")},
        "sector2": {r[0]: r[1] for r in connection.execute("SELECT sector2,COUNT(*) FROM raw_problems WHERE split='Training' GROUP BY sector2")},
    }


def verify(database: Path, dataset_root: Path, *, full_images: bool = True) -> dict:
    with closing(read_connection(database)) as connection:
        require(connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "보조 DB 무결성 검사 실패")
        require(not connection.execute("PRAGMA foreign_key_check").fetchall(), "보조 DB 외래키 검사 실패")
        for row in connection.execute("SELECT * FROM source_archives"):
            path = archive_path(dataset_root, row["relative_path"])
            require(path.stat().st_size == row["bytes"] and file_sha256(path) == row["sha256"], f"ZIP이 변경됐습니다: {row['relative_path']}")
        assets = connection.execute("SELECT * FROM source_assets ORDER BY archive_key,member").fetchall()
        require(len(assets) == connection.execute("SELECT COUNT(*)*2 FROM raw_problems").fetchone()[0], "이미지 연결 수 불일치")
        require(not connection.execute("""SELECT 1 FROM raw_problems p WHERE
            NOT EXISTS (SELECT 1 FROM source_assets a WHERE a.split=p.split AND a.question_id=p.question_id AND a.kind='question')
            OR NOT EXISTS (SELECT 1 FROM source_assets a WHERE a.split=p.split AND a.question_id=p.question_id AND a.kind='answer') LIMIT 1""").fetchone(), "문제/답안 이미지 연결 누락")
        if full_images:
            paths = {r["archive_key"]: r["relative_path"] for r in connection.execute("SELECT * FROM source_archives")}
            with ExitStack() as stack:
                opened = {key: stack.enter_context(zipfile.ZipFile(archive_path(dataset_root, path))) for key, path in paths.items() if key.endswith("_image")}
                for row in assets:
                    content = opened[row["archive_key"]].read(row["member"])
                    require(sha256(content) == row["sha256"] and len(content) == row["bytes"], f"이미지 무결성 불일치: {row['filename']}")
                    require(png_dimensions(content) == (row["width"], row["height"]), f"이미지 크기 불일치: {row['filename']}")
        report = summarize(connection)
        report.update({"integrity": "ok", "foreign_keys": "ok", "full_images_verified": full_images})
        return report


def export_inputs(database: Path, output_dir: Path) -> None:
    with closing(read_connection(database)) as connection:
        for filename, query in (
            ("train.jsonl", "SELECT * FROM classifier_training_pool ORDER BY question_id"),
            ("validation.jsonl", "SELECT * FROM raw_problems WHERE split='Validation' ORDER BY question_id"),
        ):
            with (output_dir / filename).open("x", encoding="utf-8", newline="\n") as stream:
                for row in connection.execute(query):
                    asset = dict(connection.execute("""SELECT a.*,s.relative_path FROM source_assets a
                        JOIN source_archives s USING(archive_key)
                        WHERE a.split=? AND a.question_id=? AND a.kind='question'""", (row["split"], row["question_id"])).fetchone())
                    same_input = bool(connection.execute("SELECT 1 FROM classifier_training_pool WHERE input_sha256=? LIMIT 1", (row["input_sha256"],)).fetchone()) if row["split"] == "Validation" else False
                    data = {
                        "id": row["question_id"], "split": row["split"], "grade": row["grade"],
                        "input": {"question_text": row["question_text"], "figure_text": row["figure_text"]},
                        "label": {key: row[key] for key in ("source_topic_code", "source_topic_name", "unit_code", "sector1", "sector2", "difficulty", "question_step")},
                        "question_image": asset, "input_sha256": row["input_sha256"],
                        "same_input_in_training_pool": same_input,
                    }
                    # 정답/해설/답안 이미지는 분류기 입력 파일에 넣지 않는다.
                    stream.write(packed(data) + "\n")
        with (output_dir / "topic_catalog.json").open("x", encoding="utf-8") as stream:
            json.dump([dict(row) for row in connection.execute("SELECT * FROM raw_topic_catalog ORDER BY split,grade,semester,unit_code,source_topic_code")], stream, ensure_ascii=False, indent=2)


def build(database: Path, dataset_root: Path, bank_path: Path, grades: list[int]) -> dict:
    database.parent.mkdir(parents=True, exist_ok=True)
    with database.open("xb"):
        pass
    connection = None
    try:
        connection = sqlite3.connect(database)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript("BEGIN IMMEDIATE;\n" + SCHEMA.read_text(encoding="utf-8"))
        insert_sources(connection, dataset_root.resolve(), grades, bank_path)
        require(not connection.execute("PRAGMA foreign_key_check").fetchall(), "외래키 검사 실패")
        connection.commit()
        connection.close()
        connection = None
        return verify(database, dataset_root)
    except BaseException:
        if connection is not None:
            connection.rollback()
            connection.close()
        database.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-root", type=Path, default=DEFAULT_DATASET_ROOT)
    parser.add_argument("--bank", type=Path, default=DEFAULT_BANK)
    parser.add_argument("--database", type=Path, default=DEFAULT_RAW_DB)
    parser.add_argument("--grades", type=int, nargs="+", choices=(1, 2, 3), default=[1])
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    try:
        if args.verify:
            report = verify(args.database, args.dataset_root)
        else:
            require(not args.database.exists(), "기존 보조 DB를 덮어쓰지 않습니다. --verify 또는 다른 --database 경로를 사용하세요.")
            outputs = [args.database.parent / name for name in ("train.jsonl", "validation.jsonl", "topic_catalog.json", "report.json")]
            require(not any(p.exists() for p in outputs), "기존 출력 파일이 있습니다. 다른 폴더의 --database 경로를 사용하세요.")
            report = build(args.database, args.dataset_root, args.bank, sorted(set(args.grades)))
            export_inputs(args.database, args.database.parent)
            with (args.database.parent / "report.json").open("x", encoding="utf-8") as stream:
                json.dump(report, stream, ensure_ascii=False, indent=2)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError, sqlite3.Error, zipfile.BadZipFile) as error:
        print(f"원천 데이터 준비 실패: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
