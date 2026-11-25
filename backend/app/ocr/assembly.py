"""검출 좌표와 읽기 순서를 보존해 본문과 FormulaNet 전사를 결합한다."""

from collections import defaultdict
from dataclasses import replace
from io import BytesIO
import re
from time import monotonic

from PIL import Image, ImageEnhance, ImageOps

from .images import Region, prepare
from .llama import Backend
from .retry import critical_signature


MATH = re.compile(r"\\\((.*?)\\\)|\\\[(.*?)\\\]|\$\$(.*?)\$\$|(?<!\\)\$([^$\n]+)(?<!\\)\$", re.DOTALL)
MAX_REGIONS = 32


def latex_body(text):
    text = text.strip()
    match = MATH.fullmatch(text)
    return next(group for group in match.groups() if group is not None) if match else text


def area(box):
    return (box[2] - box[0]) * (box[3] - box[1])


def intersection(a, b):
    return max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))


def plan_regions(detection):
    layout = detection["layout"]
    nodes = {node["id"]: node for node in layout["regions"]}
    previous = (detection.get("report") or {}).get("kinds_before_grouping", {})
    children = defaultdict(list)
    for node in nodes.values():
        children[node["parent_id"]].append(node)
    for siblings in children.values():
        siblings.sort(key=lambda node: node["reading_order"])
    ordered = []

    def visit(parent):
        for node in children[parent]:
            ordered.append(node)
            visit(node["id"])

    visit(None)
    order = {node["id"]: index for index, node in enumerate(ordered)}
    formulas = [node for node in ordered if node["kind"] == "formula"]
    units = []

    def collect(parent):
        for node in children[parent]:
            kind = previous.get(node["id"], node["kind"])
            if node["kind"] == "formula":
                continue
            if kind == "group":
                collect(node["id"])
            else:
                # 본문을 포함했던 부모 박스는 자식 사이의 글자도 함께 전사한다.
                units.append({**node, "kind": kind, "formula_ids": []})

    collect(None)
    # 검출기의 겹친 본문 후보는 한 번만 읽어 같은 문장을 중복 삽입하지 않는다.
    for unit in units:
        unit["source_region_ids"] = [unit["id"]]
    changed = True
    while changed:
        changed = False
        for index, first in enumerate(units):
            for second in units[index + 1:]:
                if first["kind"] == second["kind"] == "text" and (
                    intersection(first["bbox"], second["bbox"]) / min(area(first["bbox"]), area(second["bbox"])) > 0.1
                ):
                    first["bbox"] = [min(first["bbox"][0], second["bbox"][0]), min(first["bbox"][1], second["bbox"][1]),
                                     max(first["bbox"][2], second["bbox"][2]), max(first["bbox"][3], second["bbox"][3])]
                    first["source_region_ids"].extend(second["source_region_ids"])
                    if first["parent_id"] != second["parent_id"]:
                        first["parent_id"] = None
                    units.remove(second)
                    changed = True
                    break
            if changed:
                break
    for formula in formulas:
        owners = [unit for unit in units if unit["kind"] != "formula" and intersection(unit["bbox"], formula["bbox"]) / area(formula["bbox"]) >= 0.98]
        if owners:
            min(owners, key=lambda unit: area(unit["bbox"]))["formula_ids"].append(formula["id"])
        else:
            units.append({**formula, "formula_ids": [formula["id"]]})
    units.sort(key=lambda unit: order[unit["id"]])
    ambiguous = any(
        intersection(a["bbox"], b["bbox"]) / min(area(a["bbox"]), area(b["bbox"])) > 0.1
        for index, a in enumerate(units) for b in units[index + 1:]
    )
    if len(units) > MAX_REGIONS or len(formulas) > MAX_REGIONS:
        raise ValueError("자동 인식 영역이 너무 많습니다. 문제 범위를 좁혀 주세요.")
    return units, formulas, ambiguous


def merge_formulas(text, formulas):
    """순서·본문 앵커가 확인된 수식만 바꾸고 충돌하는 원문은 유지한다."""
    spans = list(MATH.finditer(text))
    replacements, used, warnings = [], set(), []
    for position, formula in enumerate(formulas):
        if not formula.get("usable"):
            continue
        body = latex_body(formula["text"])
        if len(spans) == len(formulas):
            indices = [position]
        else:
            indices = [index for index, span in enumerate(spans) if index not in used and
                       critical_signature(latex_body(span.group())) == critical_signature(body)]
        if len(indices) != 1:
            warnings.append({"code": "formula_alignment", "message": "수식의 본문 위치를 확정하지 못했습니다. 원본과 영역별 결과를 확인해 주세요."})
            continue
        index = indices[0]
        span = spans[index]
        original = latex_body(span.group())
        used.add(index)
        # 선택지 번호를 수식에 중복해서 붙이지 않는다.
        leading = re.match(r"^\\textcircled\s*\{([1-5])\}\s*", body)
        if leading and not original.lstrip().startswith(r"\textcircled"):
            label = "①②③④⑤"[int(leading.group(1)) - 1]
            if text[:span.start()].rstrip().endswith(label):
                body = body[leading.end():]
            else:
                warnings.append({"code": "formula_alignment", "message": "수식에 포함된 선택지 번호의 위치를 확인해 주세요."})
                continue
        if critical_signature(original) != critical_signature(body):
            warnings.append({
                "code": "formula_conflict", "message": "본문과 FormulaNet의 수식 판독이 다릅니다. 원본과 비교해 주세요.",
                "first_text": original, "second_text": body,
                "first_label": "본문 수식 인식", "second_label": "FormulaNet 수식 인식",
            })
            continue
        inner = next(group for group in range(1, 5) if span.group(group) is not None)
        replacements.append((span.start(inner), span.end(inner), body))
        formula["accepted"] = True
    if [start for start, _, _ in replacements] != sorted(start for start, _, _ in replacements):
        for formula in formulas:
            formula["accepted"] = False
        return text, [{"code": "formula_alignment", "message": "수식의 위치와 읽기 순서를 확인해 주세요."}]
    for start, end, body in reversed(replacements):
        text = text[:start] + body + text[end:]
    return text, warnings


def preserves_prose(baseline, combined):
    def compact(text):
        return re.sub(r"[^0-9A-Za-z가-힣①②③④⑤]", "", MATH.sub("", text))
    expected, actual = compact(baseline), iter(compact(combined))
    return all(any(char == candidate for candidate in actual) for char in expected)


def prepare_region(source, node, adjust):
    kind = "formula" if node["kind"] == "formula" else "text"
    content, _ = prepare(source, Region("auto-" + node["id"], kind, tuple(node["bbox"])))
    if adjust:
        with Image.open(BytesIO(content)) as image:
            adjusted = ImageEnhance.Contrast(ImageOps.autocontrast(image.convert("RGB"))).enhance(1.3)
            try:
                output = BytesIO()
                adjusted.save(output, "PNG")
                content = output.getvalue()
            finally:
                adjusted.close()
    return content


def recognize_layout(source, detection, baseline, primary, formula_backend, read, prompt,
                     max_tokens=1024, adjust=False, deadline=None):
    deadline = min(deadline or float("inf"), monotonic() + 60)
    warnings, records, blocks = [], [], []
    output = {
        "schema_version": "region-assembly-v1", "coordinate_space": "ocr_image",
        "image_size": list(source.size), "status": "fallback", "baseline_text": baseline,
        "text": baseline, "blocks": [], "formula_results": records, "warnings": warnings,
    }
    try:
        units, formulas, ambiguous = plan_regions(detection)
        if formulas:
            yield {"type": "stage", "stage": "formula"}
            with formula_backend.connect() as client:
                for node in formulas:
                    if monotonic() >= deadline:
                        raise TimeoutError("영역별 인식 시간 예산을 초과했습니다.")
                    region = Region("auto-" + node["id"], "formula", tuple(node["bbox"]))
                    record = yield from read(client, prepare_region(source, node, adjust), "Formula Recognition:", region, "pp-formulanet-plus-m")
                    records.append({**node, **record, "layout_region_id": node["id"], "accepted": False})
        if any(not record["usable"] for record in records):
            raise ValueError("일부 수식 영역을 인식하지 못했습니다.")
        if ambiguous:
            raise ValueError("서로 겹치는 본문 영역의 결합 순서를 확정하지 못했습니다.")
        by_formula = {record["layout_region_id"]: record for record in records}
        yield {"type": "stage", "stage": "assembly"}
        for node in units:
            if monotonic() >= deadline:
                raise TimeoutError("영역별 인식 시간 예산을 초과했습니다.")
            if node["kind"] == "formula":
                record = by_formula[node["id"]]
                text = r"\(" + latex_body(record["text"]) + r"\)"
            elif node["kind"] == "unknown" and node["bbox"] == [0, 0, *source.size]:
                text = baseline
            else:
                backend = replace(primary, timeout=max(1, min(30, deadline - monotonic(), primary.timeout))) if isinstance(primary, Backend) else primary
                region = Region("auto-" + node["id"], "text", tuple(node["bbox"]))
                with backend.connect() as client:
                    record = yield from read(client, prepare_region(source, node, adjust), prompt, region, "qwen", max_tokens)
                if not record["usable"]:
                    raise ValueError("일부 본문 영역을 인식하지 못했습니다.")
                text = record["text"]
                text, extra = merge_formulas(text, [by_formula[key] for key in node["formula_ids"]])
                warnings.extend(extra)
            blocks.append({
                "region_id": node["id"], "kind": node["kind"], "bbox": node["bbox"],
                "parent_id": node["parent_id"], "reading_order": node["reading_order"],
                "order": len(blocks), "text": text,
                "formula_ids": node["formula_ids"],
                "source_region_ids": node.get("source_region_ids", [node["id"]]),
            })
        combined = "\n\n".join(block["text"] for block in blocks if block["text"].strip())
        if not combined.strip() or not preserves_prose(baseline, combined):
            raise ValueError("영역별 결합에서 본문이나 선택지가 빠졌을 수 있습니다.")
        baseline_math = list(MATH.finditer(baseline))
        if len(baseline_math) == len(records) and any(
            critical_signature(latex_body(span.group())) != critical_signature(latex_body(record["text"]))
            for span, record in zip(baseline_math, records)
        ):
            raise ValueError("전체 본문과 영역별 수식 판독의 핵심 표현이 다릅니다.")
        output.update(status="assembled", text=combined, blocks=blocks)
        for node in units:
            if node["kind"] == "formula":
                by_formula[node["id"]]["accepted"] = True
    except Exception as error:
        output["fallback_reason"] = str(error)
        warnings.append({"code": "assembly_fallback", "message": "일부 영역을 확인하지 못해 전체 본문 인식을 유지했어요. 수식과 선택지를 원본과 비교해 주세요."})
        for record in records:
            record["accepted"] = False
        output["text"], extra = merge_formulas(baseline, records)
        warnings.extend(extra)
        output["blocks"] = [{
            "region_id": "problem", "kind": "text", "bbox": [0, 0, *source.size],
            "parent_id": None, "reading_order": 0, "order": 0, "text": output["text"],
        }]
        output["candidate_blocks"] = blocks
    yield {"type": "assembly_complete", "status": output["status"], "formula_count": len(records)}
    return output
