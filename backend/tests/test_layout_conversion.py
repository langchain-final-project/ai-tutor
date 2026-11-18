"""영역 후보 정리에서 기호·원본 근거·좌표 관계가 보존되는지 검증한다."""

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

from PIL import Image
import pytest

from app.ocr.layout import ProblemLayout
from app.ocr.layout_conversion import convert_detections


def box(label, bbox, score=0.5):
    return {"label": label, "coordinate": bbox, "score": score}


def source_ids(report):
    return sorted(index for members in report["source_ids_by_region"].values() for index in members)


def test_lower_scoring_formula_keeps_the_leading_placeholder_and_raw_evidence():
    raw = {"res": {"boxes": [
        box("formula", [475.69, 919.99, 1101.55, 1193.97], 0.415),
        box("formula", [297.62, 921.57, 1102.08, 1192.46], 0.389),
    ]}}
    before = deepcopy(raw)
    layout, report = convert_detections(raw, (3000, 4000))
    formulas = [region for region in layout.regions if region.kind == "formula"]
    assert len(formulas) == 1
    assert formulas[0].bbox == (297, 919, 1103, 1194)
    assert report["merged_count"] == 1
    assert source_ids(report) == [0, 1]
    assert report["raw_detections"] == before
    assert raw == before


def test_separate_or_much_smaller_formulas_are_not_merged():
    layout, report = convert_detections({"boxes": [
        box("formula", [0, 0, 40, 40]),
        box("formula", [60, 0, 100, 40]),
        box("formula", [5, 5, 10, 10]),
    ]}, (100, 100))
    assert sum(region.kind == "formula" for region in layout.regions) == 3
    assert report["merged_count"] == 0
    assert source_ids(report) == [0, 1, 2]


def test_same_geometry_with_different_kinds_remains_ambiguous():
    layout, report = convert_detections({"boxes": [
        box("text", [10, 10, 60, 30]),
        box("formula", [10, 10, 60, 30]),
    ]}, (100, 100))
    assert {region.kind for region in layout.regions} == {"group", "text", "formula"}
    assert all(region.role == "unknown" for region in layout.regions)
    assert report["merged_count"] == 0


def test_geometric_parent_expands_without_losing_child_pixels():
    layout, report = convert_detections({"boxes": [
        box("text", [10, 10, 90, 40]),
        box("formula", [20, 12, 91, 39]),
        box("text", [15, 12, 18, 39]),
    ]}, (100, 100))
    nodes = {region.id: region for region in layout.regions}
    formula = next(region for region in layout.regions if region.kind == "formula")
    parent = nodes[formula.parent_id]
    assert parent.kind == "group"
    assert parent.bbox == (10, 10, 91, 40)
    assert report["kinds_before_grouping"][parent.id] == "text"
    assert source_ids(report) == [0, 1, 2]
    assert ProblemLayout.model_validate_json(layout.model_dump_json()) == layout


def test_reading_order_uses_position_not_detector_score():
    layout, report = convert_detections({"boxes": [
        box("text", [10, 70, 90, 90], 0.99),
        box("text", [10, 10, 90, 30], 0.4),
    ]}, (100, 100))
    text = [region for region in layout.regions if region.kind == "text"]
    assert [region.bbox[1] for region in text] == [10, 70]
    assert [region.reading_order for region in text] == [0, 1]
    assert report["raw_detections"]["boxes"][0]["score"] == 0.99


def test_unknown_labels_and_empty_detections_are_retained():
    unknown, report = convert_detections({"boxes": [
        box("unrecognized_label", [-0.5, -0.5, 100.4, 100.4]),
    ]}, (100, 100))
    assert unknown.regions[1].kind == "unknown"
    assert unknown.regions[1].bbox == (0, 0, 100, 100)
    assert source_ids(report) == [0]
    empty, report = convert_detections({"boxes": []}, (100, 100))
    assert empty.regions[1].kind == "unknown"
    assert empty.regions[1].bbox == (0, 0, 100, 100)
    assert report["merged_count"] == 0


@pytest.mark.parametrize("coordinates", [
    [1, 2, 1, 3], [200, 0, 300, 10], [0, 0, float("nan"), 10],
    [0, 0, float("inf"), 10], [0, 0, True, 10], [0, 0, "5", 10], [0, 0, 5],
])
def test_invalid_candidates_are_not_silently_discarded(coordinates):
    with pytest.raises(ValueError):
        convert_detections({"boxes": [box("text", coordinates)]}, (100, 100))


def test_region_limit_does_not_silently_truncate_detections():
    raw = {"boxes": [box("text", [i * 2, 0, i * 2 + 1, 1]) for i in range(256)]}
    with pytest.raises(ValueError, match="layout-v1"):
        convert_detections(raw, (1000, 100))
    assert len(raw["boxes"]) == 256


def test_cli_writes_separate_outputs_without_overwriting_open_preview(tmp_path):
    source = tmp_path / "detections.json"
    raw = {"res": {"boxes": [box("formula", [20, 20, 80, 60])]}}
    source.write_text(json.dumps(raw), encoding="utf-8")
    with Image.new("RGB", (100, 100), "white") as image:
        image.save(tmp_path / "ocr-image.png")
    script = Path(__file__).resolve().parents[1] / "scripts/convert_problem_layout.py"
    command = [sys.executable, "-X", "utf8", "-B", str(script), str(source)]
    first = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert first.returncode == 0, first.stderr
    output = next(tmp_path.glob("converted-*"))
    original = (output / "layout-preview.png").read_bytes()
    with (output / "layout-preview.png").open("rb"):
        second = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert second.returncode == 0, second.stderr
    assert len(list(tmp_path.glob("converted-*"))) == 2
    assert (output / "layout-preview.png").read_bytes() == original
    assert json.loads(source.read_text(encoding="utf-8")) == raw
    layout = ProblemLayout.model_validate_json((output / "layout.json").read_text(encoding="utf-8"))
    assert layout.regions[1].kind == "formula"
    with Image.open(output / "layout-preview.png") as preview:
        assert preview.getpixel((20, 20)) == (255, 0, 0)
