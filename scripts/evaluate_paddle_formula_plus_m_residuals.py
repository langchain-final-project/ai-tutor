# -*- coding: utf-8 -*-
import csv
import json
import re
from pathlib import Path
from time import perf_counter

from paddleocr import FormulaRecognition


ROOT = Path(__file__).resolve().parents[1]

INPUT_PATH = ROOT / "results" / "integrated_formula_results.csv"
RESULT_PATH = (
    ROOT / "results" / "paddle_formula_plus_m_residual_results.csv"
)


def normalize_tolerant(text):
    text = "".join((text or "").split())
    text = text.replace(r"\left", "")
    text = text.replace(r"\right", "")
    text = text.replace(r"\dfrac", r"\frac")

    for command in (
        "mathbf",
        "mathrm",
        "mathit",
        "mathsf",
        "mathtt",
        "boldsymbol",
    ):
        pattern = rf"\\{command}\{{([^{{}}]*)\}}"

        previous = None

        while previous != text:
            previous = text
            text = re.sub(pattern, r"\1", text)

    text = text.replace(r"\,", "")
    text = text.replace(r"\!", "")
    text = text.replace(r"\;", "")
    text = text.replace(r"\:", "")
    text = text.replace("{", "")
    text = text.replace("}", "")

    return text


with INPUT_PATH.open(
    encoding="utf-8-sig",
    newline="",
) as file:
    all_rows = list(csv.DictReader(file))

# 현재 pix2tex + binary_2x에서도 실패한 수식만 선택
target_rows = [
    row
    for row in all_rows
    if normalize_tolerant(row["expected_latex"])
    != normalize_tolerant(row["ocr_latex"])
]

print(f"비교 대상 수식 수: {len(target_rows)}")

model = FormulaRecognition(
    model_name="PP-FormulaNet_plus-M",
    device="cpu",
)

results = []

for index, row in enumerate(target_rows, start=1):
    image_path = ROOT / row["crop_path"]
    started = perf_counter()

    try:
        predictions = list(
            model.predict(
                input=str(image_path),
                batch_size=1,
            )
        )

        recognized = ""

        for prediction in predictions:
            data = prediction.json

            if isinstance(data, str):
                data = json.loads(data)

            data = data.get("res", data)
            recognized = data.get("rec_formula", "")

        error = ""

    except Exception as exception:
        recognized = ""
        error = str(exception)

    seconds = round(perf_counter() - started, 3)

    matched = (
        normalize_tolerant(row["expected_latex"])
        == normalize_tolerant(recognized)
    )

    results.append({
        "case_id": row["case_id"],
        "block_order": row["block_order"],
        "expected_latex": row["expected_latex"],
        "pix2tex_latex": row["ocr_latex"],
        "paddle_formula_latex": recognized,
        "paddle_match": matched,
        "seconds": seconds,
        "error": error,
        "crop_path": row["crop_path"],
    })

    print(
        f"[{index}/{len(target_rows)}] "
        f"{row['case_id']} | "
        f"match={matched}"
    )

fieldnames = list(results[0].keys()) if results else []

with RESULT_PATH.open(
    "w",
    encoding="utf-8-sig",
    newline="",
) as file:
    writer = csv.DictWriter(file, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(results)

matched_count = sum(
    str(row["paddle_match"]).lower() == "true"
    for row in results
)

print()
print(f"pp-FormulaNet_plus-M 일치: {matched_count}/{len(results)}")
print(f"결과 저장: {RESULT_PATH}")