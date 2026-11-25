"""저장된 검출 결과를 변환하고 JSON과 미리보기를 생성한다. 모델은 실행하지 않는다."""

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from PIL import Image, ImageDraw

from app.ocr.layout_conversion import convert_detections


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("detections", nargs="?", type=Path, help="생략하면 가장 최근 detections.json 사용")
    args = parser.parse_args()
    source = args.detections
    if source is None:
        files = list((ROOT / ".venv/layout-check").rglob("detections.json"))
        if not files:
            parser.error("검출 결과가 없습니다. inspect_problem_layout.py를 먼저 실행해 주세요.")
        source = max(files, key=lambda path: path.stat().st_mtime)

    raw = json.loads(source.read_text(encoding="utf-8"))
    image_path = source.parent / "ocr-image.png"
    with Image.open(image_path) as image:
        layout, report = convert_detections(raw, image.size)
        output = source.parent / ("converted-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
        output.mkdir(exist_ok=False)
        (output / "layout.json").write_text(layout.model_dump_json(indent=2), encoding="utf-8")
        (output / "layout-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8",
        )

        # 새 폴더에 저장해 열려 있는 이전 미리보기와 충돌하지 않게 한다.
        with image.convert("RGB") as preview:
            draw = ImageDraw.Draw(preview)
            width = max(2, round(image.width / 500))
            colors = {"group": "gray", "text": "blue", "formula": "red", "unknown": "orange"}
            for region in layout.regions:
                if region.id == "image-root":
                    continue
                color = colors.get(region.kind, "green")
                draw.rectangle(region.bbox, outline=color, width=width)
                draw.text(
                    (region.bbox[0] + 4, region.bbox[1] + 4),
                    f"{region.id}: {region.kind}", fill=color,
                )
            preview.save(output / "layout-preview.png")

    print("Source:", source.resolve())
    print("raw_count:", report["raw_count"])
    print("merged_count:", report["merged_count"])
    print("region_count:", report["region_count"])
    print("formula_count:", sum(region.kind == "formula" for region in layout.regions))
    print("Output:", output.resolve())


if __name__ == "__main__":
    main()
