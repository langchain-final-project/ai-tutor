"""원본 문제/답안 조회 및 선택한 영역 PNG 내보내기."""

import argparse
import json
from pathlib import Path
import sqlite3
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
from app.db.raw_problem_data import DEFAULT_DATASET_ROOT, DEFAULT_RAW_DB, RawProblemStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", required=True, dest="question_id")
    parser.add_argument("--split", choices=("Training", "Validation"), default="Training")
    parser.add_argument("--kind", choices=("question", "answer"), default="question")
    parser.add_argument("--database", type=Path, default=DEFAULT_RAW_DB)
    parser.add_argument("--dataset-root", type=Path, default=DEFAULT_DATASET_ROOT)
    parser.add_argument("--list-regions", action="store_true")
    parser.add_argument("--region", type=int)
    parser.add_argument("--block", type=int, default=0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        store = RawProblemStore(args.database, args.dataset_root)
        if args.region is not None:
            if args.output is None:
                parser.error("--region에는 --output이 필요합니다.")
            result = store.crop_region(args.question_id, args.output, kind=args.kind, split=args.split,
                                       block_index=args.block, region_index=args.region)
        elif args.output:
            content = store.read_image(args.question_id, args.kind, args.split)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("xb") as stream:
                stream.write(content)
            result = {"output": str(args.output), "bytes": len(content)}
        else:
            problem = store.get_problem(args.question_id, args.split)
            result = {key: problem[key] for key in (
                "question_id", "split", "bank_question_id", "grade", "semester", "unit_code",
                "source_topic_code", "source_topic_name", "question_text", "figure_text",
                "sector1", "sector2", "question_step", "difficulty", "reserved_ocr50", "assets",
            )}
            if args.list_regions:
                result["regions"] = store.get_regions(args.question_id, args.kind, args.split)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError, RuntimeError, sqlite3.Error, zipfile.BadZipFile) as error:
        print(f"원천 문항 조회 실패: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
