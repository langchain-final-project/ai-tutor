"""출처가 기록된 이미지들을 실제 학생 API·OCR 모델로 검사한다. 개인 DB는 사용하지 않는다."""

import argparse
import hashlib
from io import BytesIO
import json
import logging
import os
from pathlib import Path
import runpy
import sys
from tempfile import TemporaryDirectory
from time import monotonic
from urllib.request import urlopen
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.engine import URL

from app.db.auth import metadata
from app.db.connection import build_engine
from app.student_api import create_student_app


def read_events(text):
    events = []
    for frame in text.split("\n\n"):
        fields = {}
        for line in frame.splitlines():
            if line and not line.startswith(":"):
                key, value = line.split(":", 1)
                fields[key] = value.lstrip(" ")
        if "event" in fields:
            fields["data"] = json.loads(fields["data"])
            fields["id"] = int(fields["id"])
            events.append(fields)
    return events


def load_cases(manifests):
    cases = []
    for manifest in manifests:
        source = json.loads(manifest.read_text(encoding="utf-8"))
        for case in source["cases"]:
            case = dict(case, manifest=str(manifest.resolve()))
            image = Path(case["image_path"])
            case["image_path"] = str(image if image.is_absolute() else manifest.parent / image)
            cases.append(case)
    if len({case["id"] for case in cases}) != len(cases):
        raise ValueError("중복된 검사 이미지 ID가 있습니다.")
    if not 1 <= len(cases) <= 12:
        raise ValueError("개발 검사는 한 번에 1~12장으로 제한합니다.")
    return cases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--qwen-url", default=os.getenv("DIDGO_QWEN_URL", "http://127.0.0.1:11434/v1"))
    parser.add_argument("--qwen-model-name", default=os.getenv("DIDGO_QWEN_MODEL"), required=not os.getenv("DIDGO_QWEN_MODEL"))
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    cases = load_cases(args.manifest)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any((output / f"{case['id']}.result.json").exists() for case in cases):
        raise ValueError("검사 결과가 이미 존재합니다. 새 출력 폴더를 지정해 주세요.")
    logging.basicConfig(filename=output / "service.log", level=logging.WARNING)
    os.environ["DIDGO_QWEN_URL"] = args.qwen_url
    os.environ["DIDGO_QWEN_MODEL"] = args.qwen_model_name
    os.environ["DIDGO_OCR_TIMEOUT"] = str(args.timeout)

    # 설치 목록을 읽을 뿐 모델을 내려받지 않는다. 정확한 digest를 결과와 함께 기록한다.
    parsed = urlsplit(args.qwen_url)
    tags_url = f"{parsed.scheme}://{parsed.netloc}/api/tags"
    with urlopen(tags_url, timeout=10) as response:
        models = json.load(response)["models"]
    selected = next((model for model in models if model["name"] == args.qwen_model_name), None)
    if selected is None:
        raise ValueError("요청한 모델이 설치 목록에 없습니다. 자동 다운로드하지 않습니다.")

    setup = runpy.run_path(str(ROOT / "backend/scripts/setup_solution_flow.py"))
    results = []
    with TemporaryDirectory(prefix="didgo-live-samples-") as directory:
        database = Path(directory) / "students.sqlite3"
        setup["prepare"](database, create_empty=True)
        engine = build_engine(URL.create("sqlite", database=str(database)))
        metadata.create_all(engine)
        engine.dispose()
        app = create_student_app(database)
        app.state.ocr_jobs.model_version = selected["digest"]
        with TestClient(app) as client:
            password = "temporary-ocr-sample-check"
            response = client.post("/auth/signup", json={"name": "OCR 검사", "email": "samples@example.com",
                "password": password, "password_confirmation": password})
            response.raise_for_status()
            client.post("/auth/login", json={"email": "samples@example.com", "password": password}).raise_for_status()
            for index, case in enumerate(cases, 1):
                image_path = Path(case["image_path"])
                content = image_path.read_bytes()
                digest = hashlib.sha256(content).hexdigest()
                expected = case.get("image_sha256") or case.get("sha256")
                if expected and digest != expected:
                    raise ValueError(f"원본 이미지 해시가 다릅니다: {case['id']}")
                with Image.open(BytesIO(content)) as image:
                    width, height = image.size
                print(f"[{index}/{len(cases)}] {case['id']} 실제 인식 시작 ({width}x{height})", flush=True)
                started = monotonic()
                upload = client.post("/submissions/photo", files={"files": (image_path.name, content, "image/png")})
                upload.raise_for_status()
                upload_seconds = monotonic() - started
                submission_id = upload.json()["id"]
                base = f"/submissions/{submission_id}"
                region = {"coordinate_space": "exif_transposed", "image_size": [width, height],
                          "bbox": [0, 0, width, height]}
                recognition_started = monotonic()
                job = client.post(base + "/ocr", json={"region": region})
                job.raise_for_status()
                job_id = job.json()["id"]
                stream = client.get(f"/ocr-jobs/{job_id}/events")
                stream.raise_for_status()
                recognition_seconds = monotonic() - recognition_started
                events = read_events(stream.text)
                current = client.get(base).json()
                history = client.get(base + "/ocr/history").json()
                saved = history[-1] if history else None
                ids = [event["id"] for event in events]
                if ids != sorted(set(ids)):
                    raise AssertionError("OCR 이벤트 순서 오류")
                if current["latest_input"] and current["latest_input"]["confirmed_at"] is not None:
                    raise AssertionError("사용자 확인 없이 OCR 입력이 확인되었습니다.")
                status = client.get(f"/ocr-jobs/{job_id}").json()["status"]
                result = {"case": case, "model": selected, "model_url": args.qwen_url,
                    "image_sha256": digest, "image_size": [width, height], "elapsed_seconds": round(monotonic()-started, 3),
                    "upload_seconds": round(upload_seconds, 3), "recognition_request_seconds": round(recognition_seconds, 3),
                    "job_status": status, "ocr_text": saved["ocr_text"] if saved else "",
                    "ocr_history": saved, "event_count": len(events),
                    "delta_count": sum(event["event"] == "delta" for event in events),
                    "review_status": current["review_status"], "automatic_confirmation": False}
                target = output / f"{case['id']}.result.json"
                with target.open("x", encoding="utf-8") as stream:
                    json.dump(result, stream, ensure_ascii=False, indent=2)
                results.append({"id": case["id"], "category": case["category"], "status": status,
                    "elapsed_seconds": result["elapsed_seconds"], "result_file": str(target)})
                print(f"  {status} / {result['elapsed_seconds']}초 / delta {result['delta_count']}개", flush=True)
    (output / "summary.json").write_text(json.dumps({"model": selected, "model_url": args.qwen_url,
        "cases": results, "limits": "소량 개발 표본. completed는 OCR 처리 완료이며 인식 정확도를 의미하지 않습니다. 사용자 DB는 사용하지 않았습니다."}, ensure_ascii=False, indent=2), encoding="utf-8")
    print("모든 검사 완료: 실제 학생 세션·사진 등록·OCR·저장 확인, 임시 DB 정리 완료", flush=True)


if __name__ == "__main__":
    main()
