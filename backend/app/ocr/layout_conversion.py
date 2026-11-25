"""영역 검출 후보를 ProblemLayout으로 변환한다.

중복 병합·부모 관계·읽기 순서는 초기 운용 규칙이다. 의미 역할은 확정하지
않으며, 원본 검출 후보를 보고서에 보존한다. 이 결과만으로 OCR 실행 영역을
선택하거나 본문을 대체하지 않는다.
"""

from collections import defaultdict
from dataclasses import dataclass
import math

from app.ocr.layout import LayoutRegion, ProblemLayout


LABEL_KIND = {
    "text": "text",
    "paragraph_title": "text",
    "doc_title": "text",
    "number": "text",
    "header": "text",
    "footer": "text",
    "figure_title": "text",
    "table_title": "text",
    "abstract": "text",
    "formula": "formula",
    "table": "table",
    "image": "figure",
    "figure": "figure",
    "chart": "graph",
}


@dataclass
class Candidate:
    kind: str
    bbox: list[int]
    source_ids: list[int]


def _area(box):
    return (box[2] - box[0]) * (box[3] - box[1])


def _intersection(a, b):
    width = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    height = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    return width * height


def _union_box(a, b):
    return [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]


def _duplicate(a: Candidate, b: Candidate) -> bool:
    if a.kind != b.kind:
        return False
    overlap = _intersection(a.bbox, b.bbox)
    iou = overlap / (_area(a.bbox) + _area(b.bbox) - overlap)
    coverage = overlap / min(_area(a.bbox), _area(b.bbox))
    # 수식도 크기가 크게 다른 별도 표현을 무조건 합치지는 않는다.
    return iou >= 0.9 or (a.kind == "formula" and coverage >= 0.98 and iou >= 0.7)


def _normalize_box(box, source_id, width, height):
    if not isinstance(box, dict) or not isinstance(box.get("label"), str):
        raise ValueError(f"후보 {source_id}: 검출 라벨이 올바르지 않습니다.")
    values = box.get("coordinate")
    if not isinstance(values, (list, tuple)) or len(values) != 4:
        raise ValueError(f"후보 {source_id}: 좌표 4개가 필요합니다.")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in values):
        raise ValueError(f"후보 {source_id}: 좌표는 숫자여야 합니다.")
    try:
        left, top, right, bottom = map(float, values)
    except (OverflowError, ValueError) as error:
        raise ValueError(f"후보 {source_id}: 유한한 좌표가 필요합니다.") from error
    if not all(math.isfinite(v) for v in (left, top, right, bottom)):
        raise ValueError(f"후보 {source_id}: 유한한 좌표가 필요합니다.")
    # 선택 범위와 같은 경계 규칙: 바깥으로 반올림한 정수, 오른쪽·아래 제외.
    bbox = [
        max(0, min(width, math.floor(left))),
        max(0, min(height, math.floor(top))),
        max(0, min(width, math.ceil(right))),
        max(0, min(height, math.ceil(bottom))),
    ]
    if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        raise ValueError(f"후보 {source_id}: 영역이 비어 있거나 이미지 밖에 있습니다.")
    return bbox


def convert_detections(raw: dict, image_size: tuple[int, int]) -> tuple[ProblemLayout, dict]:
    """방향 보정 후 검출에 사용한 이미지 크기로 후보를 검증·변환한다."""
    if len(image_size) != 2 or any(type(v) is not int or v <= 0 for v in image_size):
        raise ValueError("이미지 크기는 양의 정수 2개여야 합니다.")
    if not isinstance(raw, dict):
        raise ValueError("검출 결과는 JSON 객체여야 합니다.")
    width, height = image_size
    data = raw.get("res", raw)
    if not isinstance(data, dict) or not isinstance(data.get("boxes"), list):
        raise ValueError("검출 결과에 boxes 목록이 필요합니다.")
    boxes = data["boxes"]
    if len(boxes) > 512:
        raise ValueError("검출 후보가 너무 많습니다. 원본 결과를 확인해 주세요.")

    candidates = []
    for source_id, box in enumerate(boxes):
        bbox = _normalize_box(box, source_id, width, height)
        candidates.append(Candidate(LABEL_KIND.get(box["label"], "unknown"), bbox, [source_id]))

    changed = True
    while changed:
        changed = False
        for i in range(len(candidates)):
            for j in range(i + 1, len(candidates)):
                if _duplicate(candidates[i], candidates[j]):
                    a, b = candidates[i], candidates[j]
                    candidates[i] = Candidate(
                        a.kind, _union_box(a.bbox, b.bbox), sorted(a.source_ids + b.source_ids),
                    )
                    candidates.pop(j)
                    changed = True
                    break
            if changed:
                break

    merged_count = len(boxes) - len(candidates)
    if not candidates:
        candidates = [Candidate("unknown", [0, 0, width, height], [])]
    if len(candidates) > 255:
        raise ValueError("layout-v1의 최대 영역 수를 초과했습니다. 후보는 삭제하지 않았습니다.")

    # 면적이 더 큰 박스만 부모 후보로 삼아 순환 관계를 방지한다.
    parents = {}
    tolerance = max(2, round(min(width, height) * 0.002))
    for i, child in enumerate(candidates):
        possible = []
        for j, parent in enumerate(candidates):
            if i == j or parent.kind not in {"text", "unknown", "table", "figure", "graph"}:
                continue
            if _area(parent.bbox) <= _area(child.bbox) * 1.02:
                continue
            pl, pt, pr, pb = parent.bbox
            left, top, right, bottom = child.bbox
            if (
                pl - tolerance <= left and pt - tolerance <= top
                and right <= pr + tolerance and bottom <= pb + tolerance
                and _intersection(parent.bbox, child.bbox) / _area(child.bbox) >= 0.98
            ):
                possible.append(j)
        parents[i] = min(possible, key=lambda j: _area(candidates[j].bbox)) if possible else None

    children = defaultdict(list)
    for index, parent in parents.items():
        children[parent].append(index)

    def expand(index):
        node = candidates[index]
        for child_index in children[index]:
            expand(child_index)
            node.bbox = _union_box(node.bbox, candidates[child_index].bbox)

    for root in children[None]:
        expand(root)

    kinds_before_grouping = {}
    for index, node in enumerate(candidates):
        if node.kind in {"text", "unknown"} and len(children[index]) >= 2:
            kinds_before_grouping[f"region-{index + 1:03d}"] = node.kind
            node.kind = "group"

    for siblings in children.values():
        rows = []
        for index in sorted(siblings, key=lambda i: (candidates[i].bbox[1], candidates[i].bbox[0], i)):
            box = candidates[index].bbox
            row = next((row for row in rows if (
                min(row["bottom"], box[3]) - max(row["top"], box[1])
            ) / min(row["bottom"] - row["top"], box[3] - box[1]) >= 0.5), None)
            if row is None:
                rows.append({"top": box[1], "bottom": box[3], "members": [index]})
            else:
                row["members"].append(index)
                row["top"], row["bottom"] = min(row["top"], box[1]), max(row["bottom"], box[3])
        siblings[:] = [index for row in rows for index in sorted(row["members"], key=lambda i: (candidates[i].bbox[0], i))]

    regions = [LayoutRegion(
        id="image-root", kind="group", role="unknown",
        bbox=(0, 0, width, height), reading_order=0,
    )]
    sources = {}

    def emit(parent):
        for order, index in enumerate(children[parent]):
            node = candidates[index]
            region_id = f"region-{index + 1:03d}"
            regions.append(LayoutRegion(
                id=region_id, kind=node.kind, role="unknown", bbox=tuple(node.bbox),
                parent_id="image-root" if parent is None else f"region-{parent + 1:03d}",
                reading_order=order,
            ))
            sources[region_id] = node.source_ids
            emit(index)

    emit(None)
    layout = ProblemLayout(image_size=image_size, regions=regions)
    report = {
        "rules_version": "layout-conversion-v2",
        "raw_count": len(boxes),
        "merged_count": merged_count,
        "region_count": len(regions),
        "source_ids_by_region": sources,
        "kinds_before_grouping": kinds_before_grouping,
        "raw_detections": raw,
        "notes": [
            "중복 병합과 부모 관계는 실험용 규칙입니다.",
            "부모 관계는 좌표상 포함 관계이며 의미상 소속이 확정된 것은 아닙니다.",
            "읽기 순서는 위에서 아래이며, 세로로 절반 이상 겹치는 같은 행에서는 왼쪽에서 오른쪽입니다.",
            "묶음 안에 자식 박스가 덮지 못한 본문이 있을 수 있어 원문 OCR을 유지해야 합니다.",
            "서로 다른 종류의 겹친 후보는 검토를 위해 보존했습니다.",
        ],
    }
    return layout, report
