"""사진의 영역 검출 결과를 JSON과 박스 이미지로 저장한다."""

import argparse
import json
import os
from pathlib import Path
import sys
from datetime import datetime

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

# 다운로드한 모델과 캐시를 프로젝트 안에 보관합니다.
os.environ["PADDLE_PDX_CACHE_HOME"] = str(
    ROOT / ".venv/layout-model-cache"
)
os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"

from PIL import ImageDraw
from paddleocr import LayoutDetection

from app.ocr.images import open_image
from app.ocr.orientation import get_orientation_detector


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    args = parser.parse_args()

    run_name = (
        f"{args.image.stem}-"
        f"{datetime.now():%Y%m%d-%H%M%S-%f}"
    )

    output_dir = ROOT / ".venv/layout-check" / run_name
    output_dir.mkdir(parents=True, exist_ok=False)

    # EXIF 처리와 방향 보정 후의 사진을 검출 기준으로 사용합니다.
    with open_image(args.image.read_bytes()) as original:
        orientation = get_orientation_detector().detect(original)

        with original.rotate(
            orientation["rotation_ccw"],
            expand=True,
        ) as image:
            input_path = output_dir / "ocr-image.png"
            image.save(input_path)

            print(
                "Orientation:",
                orientation["status"],
                "rotation:",
                orientation["rotation_ccw"],
                flush=True,
            )

            model = LayoutDetection(
                model_name="PP-DocLayout_plus-L",
                device="cpu",
                enable_mkldnn=False,
                cpu_threads=2,
                threshold=0.3,
                layout_merge_bboxes_mode="union",
            )

            results = list(
                model.predict(
                    input=str(input_path),
                    batch_size=1,
                )
            )

            if len(results) != 1:
                raise RuntimeError(
                    "사진 한 장의 검출 결과를 받지 못했습니다."
                )

            raw = results[0].json

            if isinstance(raw, str):
                raw = json.loads(raw)

            data = raw.get("res", raw)
            boxes = data.get("boxes", [])

            (output_dir / "detections.json").write_text(
                json.dumps(raw, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            # 검출한 위치를 같은 기준 사진 위에 표시합니다.
            with image.copy() as preview:
                draw = ImageDraw.Draw(preview)
                line_width = max(2, round(image.width / 500))

                for index, box in enumerate(boxes, start=1):
                    label = box["label"]
                    score = float(box["score"])
                    left, top, right, bottom = map(
                        float, box["coordinate"]
                    )

                    left = max(0, min(image.width, left))
                    top = max(0, min(image.height, top))
                    right = max(0, min(image.width, right))
                    bottom = max(0, min(image.height, bottom))

                    if right <= left or bottom <= top:
                        continue

                    color = (
                        "red"
                        if "formula" in label.lower()
                        else "blue"
                    )

                    draw.rectangle(
                        (left, top, right, bottom),
                        outline=color,
                        width=line_width,
                    )

                    draw.text(
                        (left + 4, top + 4),
                        f"{index}: {label} {score:.2f}",
                        fill=color,
                    )

                    print(
                        index,
                        label,
                        round(score, 3),
                        box["coordinate"],
                    )

                preview.save(output_dir / "layout-preview.png")

            print("Detected boxes:", len(boxes))
            print("Output:", output_dir)


if __name__ == "__main__":
    main()