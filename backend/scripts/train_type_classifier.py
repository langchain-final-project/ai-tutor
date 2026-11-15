"""공유 DB로 중1~중3 모델을 학습한다. 현재 데이터와 같은 기존 모델은 검증 후 재사용한다."""

import argparse
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.tutor.classification_contract import SUPPORTED_GRADES
from app.tutor.type_classifier import DEFAULT_MODELS_DIR
from app.tutor.type_classifier_training import DEFAULT_DATABASE, train_classifier, train_school_classifiers


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--grades", nargs="+", type=int, choices=SUPPORTED_GRADES,
                        help="학습할 학년. 기본은 1 2 3")
    parser.add_argument("--model-root", type=Path, default=DEFAULT_MODELS_DIR,
                        help="학년별 middle1-v1, middle2-v1, middle3-v1 폴더를 담을 경로")
    parser.add_argument("--output-dir", type=Path,
                        help="단일 학년의 별도 저장 폴더. 학년 생략 시 기존 방식대로 중1")
    args = parser.parse_args()
    try:
        grades = args.grades or ([1] if args.output_dir else list(SUPPORTED_GRADES))
        if args.output_dir:
            if len(grades) != 1:
                raise ValueError("--output-dir는 단일 학년에만 사용합니다. 여러 학년은 --model-root를 사용해 주세요.")
            result = train_classifier(args.database, args.output_dir, grade=grades[0],
                                      progress=lambda mode: print(f"중{grades[0]} 학습: {mode}", file=sys.stderr, flush=True))
        else:
            result = train_school_classifiers(
                args.database, args.model_root, grades=tuple(grades),
                progress=lambda grade, mode: print(f"{'학년 추정' if grade == 0 else f'중{grade}'}: {mode}",
                                                   file=sys.stderr, flush=True),
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"학습 실패: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
