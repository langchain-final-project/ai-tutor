"""임시 DB와 실제 Qwen 서버로 OCR·SSE·재연결을 확인한다."""

import argparse
from contextlib import closing
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, ImageFont

from app.ocr import Backend, OCRPipeline
from app.submission_api import create_submission_app


def parse_events(text):
    events = []
    for frame in text.split("\n\n"):
        fields = {}
        for line in frame.split("\n"):
            if line and not line.startswith(":"):
                key, value = line.split(":", 1)
                fields[key] = value.lstrip(" ")
        if "event" in fields:
            fields["data"] = json.loads(fields["data"])
            if "id" in fields:
                fields["id"] = int(fields["id"])
            events.append(fields)
    return events


def sample_image():
    # 개인 사진 대신 검증용 수식 이미지를 메모리에서 만든다.
    image = Image.new("RGB", (640, 160), "white")
    ImageDraw.Draw(image).text(
        (20, 45), "Solve: 3x + 1 = 10", fill="black",
        font=ImageFont.load_default(size=32),
    )
    output = BytesIO()
    image.save(output, format="PNG")
    image.close()
    return output.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--qwen-url",
        help="실행 중인 llama.cpp 서버. 생략하면 환경변수 또는 기본 주소 사용.",
    )
    source.add_argument("--qwen-model", type=Path, help="검증용 임시 서버로 실행할 GGUF 모델")
    parser.add_argument("--qwen-model-name", help="원격 서버에서 선택할 모델 이름. 생략하면 DIDGO_QWEN_MODEL 사용")
    parser.add_argument("--qwen-mmproj", type=Path, help="GGUF 모델의 vision projector")
    parser.add_argument("--server-binary", default="llama-server")
    parser.add_argument("--image", type=Path, help="생략하면 검증용 이미지를 사용한다.")
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    if (args.qwen_model is None) != (args.qwen_mmproj is None):
        parser.error("--qwen-model과 --qwen-mmproj는 함께 지정해야 합니다.")
    if args.qwen_model is not None and args.qwen_model_name is not None:
        parser.error("--qwen-model-name은 원격 서버를 사용할 때 지정해 주세요.")
    backend_options = (
        {
            "model": args.qwen_model,
            "mmproj": args.qwen_mmproj,
            "executable": args.server_binary,
        }
        if args.qwen_model is not None
        else {
            "url": args.qwen_url
            or os.getenv("DIDGO_QWEN_URL", "http://127.0.0.1:8081/v1"),
            "model_name": args.qwen_model_name or os.getenv("DIDGO_QWEN_MODEL", "").strip() or None,
        }
    )

    content = args.image.read_bytes() if args.image else sample_image()
    filename = args.image.name if args.image else "stream-check.png"

    def pipeline_factory():
        return OCRPipeline(
            Backend(
                **backend_options,
                timeout=args.timeout,
                image_tokens=(256, 2048),
            ),
            primary_engine="qwen",
        )

    with TemporaryDirectory(prefix="didgo-stream-check-") as directory:
        database = Path(directory) / "check.sqlite3"
        with closing(sqlite3.connect(database)) as connection:
            for name in (
                "001_problem_bank.sqlite.sql",
                "002_problem_classification.sqlite.sql",
                "004_problem_submissions.sqlite.sql",
                "005_problem_ocr_jobs.sqlite.sql",
                "006_problem_ocr_regions.sqlite.sql",
            ):
                connection.executescript(
                    (ROOT / "backend/migrations" / name).read_text(encoding="utf-8")
                )

        app = create_submission_app(
            database,
            dev_token="stream-check-token",
            owner_id="stream-check-user",
            pipeline_factory=pipeline_factory,
        )
        if backend_options.get("model_name") is not None:
            app.state.ocr_jobs.model_name = backend_options["model_name"]
        headers = {"Authorization": "Bearer stream-check-token"}
        with TestClient(app) as client:
            response = client.post(
                "/submissions/photo",
                headers=headers,
                files={"files": (filename, content, "application/octet-stream")},
            )
            response.raise_for_status()
            submission_id = response.json()["id"]
            response = client.post(
                f"/submissions/{submission_id}/ocr", headers=headers
            )
            response.raise_for_status()
            job_id = response.json()["id"]
            print("사진 업로드·OCR 작업 시작 성공", flush=True)
            target = str(args.qwen_model) if args.qwen_model else backend_options["url"]
            print(f"실제 모델 호출: {target}", flush=True)

            response = client.get(f"/ocr-jobs/{job_id}/events", headers=headers)
            response.raise_for_status()
            assert response.headers["content-type"].startswith("text/event-stream")
            events = parse_events(response.text)
            assert events, "스트리밍 이벤트가 없습니다."
            if events[-1]["event"] != "completed":
                history = client.get(
                    f"/submissions/{submission_id}/ocr/history", headers=headers
                ).json()
                raise RuntimeError(f"OCR이 완료되지 않았습니다: {history}")

            deltas = [event for event in events if event["event"] == "delta"]
            assert deltas, "실제 모델의 증분 출력이 없습니다."
            ids = [event["id"] for event in events]
            assert ids == sorted(set(ids)), "이벤트 순서가 올바르지 않습니다."
            cursor = deltas[0]["id"]
            replay = client.get(
                f"/ocr-jobs/{job_id}/events",
                headers={**headers, "Last-Event-ID": str(cursor)},
            )
            replay.raise_for_status()
            assert parse_events(replay.text) == [
                event for event in events if event["id"] > cursor
            ]

            current = client.get(
                f"/submissions/{submission_id}", headers=headers
            ).json()
            assert current["review_status"] == "needs_review"
            assert current["latest_input"]["content_text"].strip()
            assert current["latest_input"]["version_number"] == 1
            assert current["latest_input"]["confirmed_at"] is None
            print(f"실제 모델 delta 이벤트: {len(deltas)}개", flush=True)
            print("완료 이벤트·입력 버전 1 저장·재연결 재생 확인", flush=True)

    print("실제 Qwen 스트리밍 검증 완료: 임시 DB 삭제, 개인 DB 변경 없음")


if __name__ == "__main__":
    main()
