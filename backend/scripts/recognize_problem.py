"""문제 이미지 한 개를 인식한다. --help로 서버·로컬 모델·영역 입력 방법을 확인한다."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ocr import Backend, OCRError, OCRPipeline, Region


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("--regions", type=Path, help='영역 JSON: [{"id":"body","kind":"text","bbox":[0,0,500,100]}]')
    parser.add_argument("--stream", action="store_true", help="진행 상황·토큰·최종 결과를 JSON Lines로 출력")
    parser.add_argument("--check-text", action="store_true", help="Qwen으로 본문을 한 번 더 읽어 차이를 표시")
    parser.add_argument("--server-binary", default="llama-server")
    parser.add_argument("--timeout", type=float, default=90)
    for name in ["paddle", "qwen"]:
        group = parser.add_mutually_exclusive_group()
        group.add_argument(f"--{name}-url", help="실행 중인 llama.cpp 서버 주소. /v1은 생략 가능")
        group.add_argument(f"--{name}-model", type=Path, help="GGUF 모델. 지정하면 서버를 자동 실행·종료")
        parser.add_argument(f"--{name}-mmproj", type=Path)
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    def backend(name, required=False):
        url, model, mmproj = (getattr(args, f"{name}_{field}") for field in ["url", "model", "mmproj"])
        if not any([url, model, mmproj]) and not required:
            return None
        if not any([url, model, mmproj]):
            url = "http://127.0.0.1:8081/v1"
        return Backend(url=url, model=model, mmproj=mmproj, executable=args.server_binary,
                       timeout=args.timeout, image_tokens=(256, 2048) if name == "qwen" else None)

    try:
        regions = None
        if args.regions:
            data = json.loads(args.regions.read_text(encoding="utf-8-sig"))
            if not isinstance(data, list) or any(not isinstance(r, dict) for r in data):
                raise ValueError("영역 JSON은 객체 목록이어야 합니다.")
            regions = [Region(id=r["id"], kind=r.get("kind", "text"),
                              bbox=tuple(r["bbox"]) if r.get("bbox") is not None else None) for r in data]
        pipeline = OCRPipeline(backend("paddle", required=True), backend("qwen"))
        result = None
        for event in pipeline.stream(args.image.read_bytes(), regions, check_text=args.check_text):
            if args.stream:
                print(json.dumps(event, ensure_ascii=False), flush=True)
            if event["type"] == "result":
                result = event["result"]
        if not args.stream:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result and result["status"] == "needs_review" else 1
    except (OCRError, OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
