"""영역 분석의 좌표·저장·실패 처리가 주 OCR에 영향을 주지 않는지 검사한다."""

from contextlib import closing, contextmanager
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import sqlite3
from unittest.mock import Mock

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
import pytest

from app.ocr.images import Region, open_image, prepare
from app.ocr.layout_conversion import convert_detections
from app.ocr.layout_detector import DocumentLayoutDetector, get_layout_detector
from app.ocr.pipeline import OCRPipeline, PROBLEM_PROMPT
from app.ocr_jobs import build_qwen_pipeline
from app.submission_api import create_submission_app

TEXT = r"다음 식 \(x^{2}=4\)를 확인하세요."
HEADERS = {"Authorization": "Bearer layout-test-token"}


def photo_bytes():
    with Image.new("RGB", (128, 96), (10, 20, 30)) as image, BytesIO() as output:
        ImageDraw.Draw(image).rectangle((3, 5, 17, 19), fill="red")
        image.save(output, format="PNG")
        return output.getvalue()


class TextBackend:
    def __init__(self, calls=None):
        self.calls = calls if calls is not None else []
        self.images = []
        self.prompts = []

    @contextmanager
    def connect(self):
        yield self

    def stream(self, image, prompt, max_tokens):
        self.calls.append("ocr")
        self.images.append(image)
        self.prompts.append(prompt)
        yield {"type": "complete", "text": TEXT, "finish_reason": "stop"}


class LayoutStub:
    def __init__(self, calls=None):
        self.calls = calls if calls is not None else []
        self.received = []

    def detect(self, image):
        self.calls.append("layout")
        self.received.append((image.size, image.tobytes()))
        width, height = image.size
        layout, report = convert_detections({"boxes": [{
            "label": "formula", "score": 0.5,
            "coordinate": [width / 4, height / 4, width * 3 / 4, height / 2],
        }]}, image.size)
        return {"model": "fixture", "status": "detected", "layout": layout.model_dump(mode="json"), "report": report}


def orientation_stub(calls=None):
    def detect(image):
        if calls is not None:
            calls.append("orientation")
        return {"status": "corrected", "rotation_ccw": 90}
    return Mock(detect=detect)


def test_layout_uses_corrected_image_before_ocr_without_changing_ocr_input():
    calls = []
    backend, layout = TextBackend(calls), LayoutStub(calls)
    content = photo_bytes()
    pipeline = OCRPipeline(backend, primary_engine="qwen", orientation_detector=orientation_stub(calls), layout_detector=layout)
    events = list(pipeline.stream(content))
    result = events[-1]["result"]
    with open_image(content) as image, image.rotate(90, expand=True) as expected:
        prepared, _ = prepare(expected, Region("problem"))
        assert layout.received == [(expected.size, expected.tobytes())]
        assert backend.images == [prepared]
    assert calls == ["orientation", "layout", "ocr"]
    assert backend.prompts == [PROBLEM_PROMPT]
    assert result["text"] == TEXT
    assert result["layout_detection"]["layout"]["image_size"] == [96, 128]
    assert result["status"] == "needs_review"
    complete = next(i for i, event in enumerate(events) if event["type"] == "layout_complete")
    qwen = next(i for i, event in enumerate(events) if event["type"] == "stage" and event["stage"] == "qwen")
    assert complete < qwen


@pytest.mark.parametrize("failure", ["exception", "missing_model", "wrong_shape", "wrong_frame", "invalid_region", "nan_report"])
def test_layout_failure_still_returns_the_same_primary_ocr(failure, tmp_path):
    backend = TextBackend()
    content = photo_bytes()
    if failure == "exception":
        detector = Mock()
        detector.detect.side_effect = RuntimeError("fixture failure")
    elif failure == "missing_model":
        detector = DocumentLayoutDetector(tmp_path / "missing-model")
    else:
        with open_image(content) as image:
            value = LayoutStub().detect(image)
        if failure == "wrong_shape":
            value = None
        elif failure == "wrong_frame":
            value["layout"]["image_size"] = [1000, 1000]
        elif failure == "invalid_region":
            value["layout"]["regions"][1]["parent_id"] = "missing"
        else:
            value["report"]["score"] = float("nan")
        detector = Mock()
        detector.detect.return_value = value
    result = OCRPipeline(backend, primary_engine="qwen", layout_detector=detector).recognize(content)
    assert result["status"] == "needs_review"
    assert result["text"] == TEXT
    assert result["layout_detection"]["status"] == "unavailable"
    assert result["layout_detection"]["layout"] is None
    assert len(backend.images) == 1
    json.dumps(result, allow_nan=False)


def test_factory_reuses_detector_and_old_callers_need_no_layout():
    assert build_qwen_pipeline().layout_detector is get_layout_detector()
    assert build_qwen_pipeline().layout_detector is build_qwen_pipeline().layout_detector
    result = OCRPipeline(TextBackend(), primary_engine="qwen").recognize(photo_bytes())
    assert result["layout_detection"] is None
    assert result["text"] == TEXT


def test_detector_converts_rgb_to_bgr_and_reuses_loaded_model():
    class Result:
        json = {"res": {"boxes": [{"label": "formula", "coordinate": [2, 2, 8, 6], "score": 0.5}]}}
    model = Mock()
    model.predict.return_value = [Result()]
    detector = DocumentLayoutDetector("unused")
    detector._model = model
    with Image.new("RGB", (10, 10), (10, 20, 30)) as image:
        for _ in range(2):
            result = detector.detect(image)
            assert result["status"] == "detected"
            assert result["layout"]["image_size"] == [10, 10]
    assert detector._model is model
    assert model.predict.call_count == 2
    assert model.predict.call_args.kwargs["input"][0, 0].tolist() == [30, 20, 10]


@pytest.fixture
def database(tmp_path):
    database = tmp_path / "layout-api.sqlite3"
    migrations = Path(__file__).resolve().parents[1] / "migrations"
    with closing(sqlite3.connect(database)) as connection:
        for name in ("001_problem_bank.sqlite.sql", "002_problem_classification.sqlite.sql",
                     "004_problem_submissions.sqlite.sql", "005_problem_ocr_jobs.sqlite.sql", "006_problem_ocr_regions.sqlite.sql"):
            connection.executescript((migrations / name).read_text(encoding="utf-8"))
    return database


def make_app(database, backend, detector, owner="A"):
    return create_submission_app(
        database, dev_token="layout-test-token", owner_id=owner,
        pipeline_factory=lambda: OCRPipeline(backend, primary_engine="qwen", layout_detector=detector, orientation_detector=orientation_stub()),
    )


@pytest.mark.parametrize("unavailable", [False, True])
def test_layout_and_crop_survive_save_confirm_and_reconnect(database, unavailable):
    backend = TextBackend()
    detector = LayoutStub()
    if unavailable:
        detector = Mock()
        detector.detect.side_effect = RuntimeError("fixture unavailable")
    app = make_app(database, backend, detector)
    region = {"coordinate_space": "exif_transposed", "image_size": [128, 96], "bbox": [10, 10, 110, 85]}
    with TestClient(app) as client:
        response = client.post("/submissions/photo", headers=HEADERS, files={"files": ("math.png", photo_bytes(), "image/png")})
        sid = response.json()["id"]
        started = client.post(f"/submissions/{sid}/ocr", headers=HEADERS, json={"region": region})
        assert started.status_code == 202, started.text
        app.state.ocr_jobs.executor.submit(lambda: None).result(timeout=10)
        current = client.get(f"/submissions/{sid}", headers=HEADERS).json()
        assert current["latest_input"]["content_text"] == TEXT
        detection = deepcopy(current["layout_detection"])
        assert detection["status"] == ("unavailable" if unavailable else "detected")
        assert current["selected_region"] == region
        if not unavailable:
            assert detection["layout"]["image_size"] == [75, 100]
            displayed = client.get(f"/submissions/{sid}/photo?selected=true&corrected=true", headers=HEADERS)
            with Image.open(BytesIO(displayed.content)) as image:
                assert list(image.size) == detection["layout"]["image_size"]

        saved = client.patch(f"/submissions/{sid}/input", headers=HEADERS, json={"content_text": "학생 수정", "expected_version_number": 1})
        assert saved.status_code == 200
        assert saved.json()["layout_detection"] == detection
        confirmed = client.post(f"/submissions/{sid}/confirm", headers=HEADERS, json={"content_text": "학생 수정", "expected_version_number": 2})
        assert confirmed.status_code == 200
        assert confirmed.json()["layout_detection"] == detection
        history = client.get(f"/submissions/{sid}/ocr/history", headers=HEADERS).json()
        assert history[0]["raw_response"]["layout_detection"] == detection
        events = client.get(f"/ocr-jobs/{started.json()['id']}/events", headers=HEADERS)
        assert "event: layout_complete" in events.text

    with TestClient(make_app(database, backend, detector)) as fresh:
        restored = fresh.get(f"/submissions/{sid}", headers=HEADERS).json()
        assert restored["layout_detection"] == detection
        assert restored["selected_region"] == region
        assert restored["latest_input"]["content_text"] == "학생 수정"
        assert restored["review_status"] == "confirmed"
        assert len(backend.images) == 1
    with TestClient(make_app(database, backend, detector, owner="B")) as other:
        assert other.get(f"/submissions/{sid}", headers=HEADERS).status_code == 404
