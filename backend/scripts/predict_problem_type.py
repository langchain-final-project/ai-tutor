"""문제 내용으로 학년과 유형 후보를 예측한다. --text는 도형·표 없는 문제 입력에 사용한다."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.tutor.classification_contract import SUPPORTED_GRADES
from app.tutor.type_classifier import DEFAULT_MODELS_DIR, MiddleSchoolTypeClassifier, TypeClassifier


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path, help="1번 단계 ClassificationRequest 형식의 JSON 파일")
    source.add_argument("--text", help="도형·표 없는 문제의 확인할 본문")
    parser.add_argument("--confirmed", action="store_true", help="--text의 본문·수식을 직접 확인했을 때 지정")
    parser.add_argument("--grade", type=int, choices=SUPPORTED_GRADES,
                        help="중1=1, 중2=2, 중3=3. 생략하면 자동 추정하고 애매할 때만 확인")
    parser.add_argument("--model-root", type=Path, default=DEFAULT_MODELS_DIR)
    parser.add_argument("--model-dir", type=Path, help="특정 단일 학년 모델 폴더를 직접 지정할 때 사용")
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()
    try:
        if args.input:
            if args.confirmed:
                raise ValueError("--input은 JSON의 user_confirmed 값을 사용합니다.")
            payload = json.loads(args.input.read_text(encoding="utf-8-sig"))
            if not isinstance(payload, dict):
                raise ValueError("입력 JSON은 객체여야 합니다.")
            if args.grade is not None:
                if payload.get("grade") is not None and payload["grade"] != args.grade:
                    raise ValueError("JSON 학년과 --grade 값이 다릅니다.")
                payload["grade"] = args.grade
        else:
            payload = {"question_text": args.text, "grade": args.grade, "has_figure": False,
                       "has_table": False, "user_confirmed": args.confirmed}
        model = TypeClassifier.load(args.model_dir) if args.model_dir else MiddleSchoolTypeClassifier(args.model_root)
        result = model.predict(payload, top_k=args.top_k)
        print(result.model_dump_json(indent=2))
    except (OSError, ValueError, KeyError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
