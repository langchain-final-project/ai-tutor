"""자동 수식 영역 인식과 위치·읽기 순서에 따른 결합·보존을 검증한다."""

from contextlib import closing, contextmanager
from io import BytesIO
from pathlib import Path
import sqlite3

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
import pytest

from app.ocr.assembly import merge_formulas, plan_regions, preserves_prose
from app.ocr.formula import get_formula_backend
from app.ocr.layout_conversion import convert_detections
from app.ocr.llama import OCRError
from app.ocr.pipeline import OCRPipeline
from app.ocr.retry import assess_result, select_result
from app.ocr_jobs import build_qwen_pipeline
from app.submission_api import create_submission_app


class SequenceBackend:
    def __init__(self, texts, error=None):
        self.texts = iter(texts)
        self.calls = []
        self.error = error

    @contextmanager
    def connect(self):
        if self.error:
            raise self.error
        yield self

    def stream(self, image, prompt, max_tokens):
        self.calls.append({"image": image, "prompt": prompt})
        yield {"type": "complete", "text": next(self.texts), "finish_reason": "stop"}


def photo():
    output = BytesIO()
    with Image.new("RGB", (320, 240), (175, 175, 175)) as image:
        ImageDraw.Draw(image).rectangle((12, 12, 250, 140), outline=(30, 30, 30))
        image.save(output, "PNG")
    return output.getvalue()


def detection(boxes):
    layout, report = convert_detections({"boxes": [
        {"label": kind, "coordinate": bbox} for kind, bbox in boxes
    ]}, (320, 240))
    return {"status": "detected", "layout": layout.model_dump(mode="json"), "report": report}


class Layout:
    def __init__(self, boxes):
        self.value = detection(boxes)

    def detect(self, image):
        assert image.size == (320, 240)
        return self.value


def pipeline(primary, formula, boxes):
    return OCRPipeline(primary, formula=formula, primary_engine="qwen", layout_detector=Layout(boxes))


def test_standalone_formula_joins_between_text_in_position_order():
    baseline = "문제\n" + r"\(x^2=4\)" + "\n마지막"
    primary = SequenceBackend([baseline, "문제", "마지막"])
    formula = SequenceBackend([r"x^{2}=4"])
    boxes = [("text", [10, 120, 300, 155]), ("formula", [10, 60, 250, 95]), ("text", [10, 10, 300, 40])]
    result = pipeline(primary, formula, boxes).recognize(photo())
    assembly = result["region_assembly"]
    assert assembly["status"] == "assembled"
    assert result["text"] == "문제\n\n" + r"\(x^{2}=4\)" + "\n\n마지막"
    assert [block["bbox"][1] for block in assembly["blocks"]] == [10, 60, 120]
    assert [block["order"] for block in assembly["blocks"]] == [0, 1, 2]
    assert assembly["formula_results"][0]["accepted"]
    assert len(formula.calls) == 1


def test_inline_formula_does_not_duplicate_or_discard_parent_words():
    baseline = r"앞부분 \(x^2=4\) 뒤에 있는 본문도 보존"
    primary = SequenceBackend([baseline, baseline])
    formula = SequenceBackend([r"x^{2}=4"])
    boxes = [("text", [10, 10, 300, 110]), ("formula", [80, 40, 160, 65])]
    result = pipeline(primary, formula, boxes).recognize(photo())
    assembly = result["region_assembly"]
    assert assembly["status"] == "assembled"
    assert len(assembly["blocks"]) == 1
    assert result["text"] == r"앞부분 \(x^{2}=4\) 뒤에 있는 본문도 보존"
    assert len(formula.calls) == 1
    with Image.open(BytesIO(formula.calls[0]["image"])) as cropped:
        assert cropped.width < 320
        assert cropped.height < 240


def test_group_that_was_text_keeps_prose_between_children():
    baseline = r"앞 \(x=1\) 중간 본문 \(y=2\) 뒤"
    primary, formula = SequenceBackend([baseline, baseline]), SequenceBackend(["x=1", "y=2"])
    boxes = [("text", [10, 10, 300, 150]), ("formula", [20, 30, 90, 50]), ("formula", [20, 80, 90, 100])]
    result = pipeline(primary, formula, boxes).recognize(photo())
    assert result["region_assembly"]["status"] == "assembled"
    assert result["text"] == baseline
    assert len(result["region_assembly"]["blocks"]) == 1
    assert len(formula.calls) == 2


def test_formula_conflict_preserves_original_and_triggers_review():
    baseline = r"문제 \(x=-3\) 끝"
    primary, formula = SequenceBackend([baseline, baseline]), SequenceBackend(["x=3"])
    boxes = [("text", [10, 10, 300, 150]), ("formula", [20, 30, 90, 50])]
    result = pipeline(primary, formula, boxes).recognize(photo())
    assert result["text"] == baseline
    assert result["region_assembly"]["status"] == "fallback"
    assert not result["region_assembly"]["formula_results"][0]["accepted"]
    assessment = assess_result(result)
    assert "formula_conflict" in assessment["retry_reasons"]
    assert any(w["code"] == "formula_conflict" for w in assessment["review_warnings"])


def test_missing_prose_falls_back_without_losing_original_text():
    baseline = r"검출되지 않은 앞부분 문제 \(x=3\) 끝"
    primary, formula = SequenceBackend([baseline, r"문제 \(x=3\) 끝"]), SequenceBackend(["x=3"])
    boxes = [("text", [10, 10, 300, 150]), ("formula", [20, 30, 90, 50])]
    result = pipeline(primary, formula, boxes).recognize(photo())
    assert result["text"] == baseline
    assert result["region_assembly"]["status"] == "fallback"
    assert result["region_assembly"]["blocks"][0]["bbox"] == [0, 0, 320, 240]


def test_missing_formula_model_is_an_advisory_fallback():
    baseline = r"문제 \(x=3\) 끝"
    primary = SequenceBackend([baseline])
    formula = SequenceBackend([], OCRError("missing model", retryable=False))
    boxes = [("text", [10, 10, 300, 150]), ("formula", [20, 30, 90, 50])]
    result = pipeline(primary, formula, boxes).recognize(photo())
    assert result["status"] == "needs_review"
    assert result["text"] == baseline
    assert assess_result(result)["retry_reasons"] == []
    assert assess_result(result)["review_warnings"][0]["code"] == "assembly_fallback"


def test_ambiguous_overlaps_do_not_duplicate_text():
    value = detection([("text", [10, 10, 200, 70]), ("text", [100, 30, 300, 90])])
    units, _, ambiguous = plan_regions(value)
    assert not ambiguous
    assert len(units) == 1
    assert units[0]["bbox"] == [10, 10, 300, 90]
    assert len(units[0]["source_region_ids"]) == 2
    mixed = detection([("text", [10, 10, 200, 70]), ("table", [100, 30, 300, 90])])
    assert plan_regions(mixed)[2]


def test_choice_label_is_not_duplicated_when_replacing_inline_formula():
    record = {"text": r"\textcircled{1}x=-3", "usable": True}
    merged, warnings = merge_formulas(r"① \(x=-3\)", [record])
    assert merged == r"① \(x=-3\)"
    assert warnings == []
    assert record["accepted"]


def test_formula_alignment_respects_order_and_rejects_ambiguous_matches():
    records = [{"text": "y=2", "usable": True}, {"text": "x=1", "usable": True}]
    baseline = r"\(x=1\) \(y=2\) \(z=3\)"
    merged, warnings = merge_formulas(baseline, records)
    assert merged == baseline
    assert warnings[0]["code"] == "formula_alignment"
    assert not any(record["accepted"] for record in records)


def test_prose_coverage_and_recovered_assembly_affect_selection():
    assert not preserves_prose(r"조건이 있다 \(x=3\)", r"\(x=3\)")
    assert preserves_prose(r"조건이 있다 \(x=3\)", r"조건이 있다 \(x=3\) 추가 조건")
    base = {"status": "needs_review", "text": r"문제 \(x=3\)", "issues": [], "regions": []}
    first = {**base, "region_assembly": {"warnings": [{"code": "formula_alignment", "message": "확인"}]}}
    second = {**base, "region_assembly": {"warnings": []}}
    assert select_result([first, second])[0] == 1


def test_factory_reuses_formula_model_without_loading_at_creation():
    assert build_qwen_pipeline().formula is get_formula_backend()
    assert build_qwen_pipeline().formula is build_qwen_pipeline().formula


def test_assembly_coordinates_and_results_survive_student_edit_and_reload(tmp_path):
    database = tmp_path / "assembly.sqlite3"
    migrations = Path(__file__).resolve().parents[1] / "migrations"
    with closing(sqlite3.connect(database)) as connection:
        for name in ("001_problem_bank.sqlite.sql", "002_problem_classification.sqlite.sql",
                     "004_problem_submissions.sqlite.sql", "005_problem_ocr_jobs.sqlite.sql",
                     "006_problem_ocr_regions.sqlite.sql"):
            connection.executescript((migrations / name).read_text(encoding="utf-8"))
    baseline = "앞\n" + r"\(x^2=4\)" + "\n뒤"
    primary, formula = SequenceBackend([baseline, "앞", "뒤"]), SequenceBackend(["x^{2}=4"])
    layout = detection([("text", [10, 10, 200, 35]), ("formula", [10, 50, 200, 80]), ("text", [10, 100, 200, 130])])
    layout["layout"]["image_size"] = [240, 320]
    layout["layout"]["regions"][0]["bbox"] = [0, 0, 240, 320]

    class RotatedLayout:
        def detect(self, image):
            assert image.size == (240, 320)
            return layout

    class Orientation:
        def detect(self, image):
            return {"status": "corrected", "rotation_ccw": 90}

    def factory():
        return OCRPipeline(primary, formula=formula, primary_engine="qwen",
                           layout_detector=RotatedLayout(), orientation_detector=Orientation())

    def app():
        return create_submission_app(database, dev_token="assembly-test", owner_id="A", pipeline_factory=factory)

    application = app()
    headers = {"Authorization": "Bearer assembly-test"}
    with TestClient(application) as client:
        uploaded = client.post("/submissions/photo", headers=headers,
                               files={"files": ("math.png", photo(), "image/png")})
        assert uploaded.status_code == 201
        sid = uploaded.json()["id"]
        region = {"coordinate_space": "exif_transposed", "image_size": [320, 240], "bbox": [0, 0, 320, 240]}
        started = client.post(f"/submissions/{sid}/ocr", headers=headers, json={"region": region})
        assert started.status_code == 202
        application.state.ocr_jobs.executor.submit(lambda: None).result(timeout=10)
        current = client.get(f"/submissions/{sid}", headers=headers).json()
        assembly = current["region_assembly"]
        assert assembly["status"] == "assembled"
        assert assembly["image_size"] == [240, 320]
        assert assembly["blocks"][1]["bbox"] == [10, 50, 200, 80]
        assert current["latest_input"]["content_text"] == "앞\n\n" + r"\(x^{2}=4\)" + "\n\n뒤"
        displayed = client.get(f"/submissions/{sid}/photo?corrected=true&selected=true", headers=headers)
        with Image.open(BytesIO(displayed.content)) as image:
            assert list(image.size) == assembly["image_size"]
        saved = client.patch(f"/submissions/{sid}/input", headers=headers,
                             json={"content_text": "학생 수정", "expected_version_number": 1})
        assert saved.status_code == 200
        assert saved.json()["region_assembly"] == assembly
        history = client.get(f"/submissions/{sid}/ocr/history", headers=headers).json()
        assert history[0]["raw_response"]["region_assembly"] == assembly
    with TestClient(app()) as client:
        restored = client.get(f"/submissions/{sid}", headers=headers).json()
        assert restored["region_assembly"] == assembly
        assert restored["latest_input"]["content_text"] == "학생 수정"
