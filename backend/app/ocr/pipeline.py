"""문제 한 개의 OCR. 학생 확인 전에는 유형 분류·풀이를 확정하지 않는다."""

from contextlib import closing
import json
import re

from .checks import compare_readings, text_issues
from .images import Region, open_image, prepare, region_box, uncovered_content
from .llama import Backend, OCRError
from .tables import parse_table


PADDLE_TASKS = {"text": "OCR:", "table": "Table Recognition:", "formula": "Formula Recognition:"}
TEXT_PROMPT = (
    "문제를 풀지 말고, 보이는 본문·보기·선택지와 수식만 그대로 옮겨 주세요. "
    "틀린 식도 수정하지 마세요. 수식은 LaTeX, 밑줄은 \\underline{내용}, "
    "빈칸은 □로 표시하고 설명은 붙이지 마세요."
)


PROBLEM_PROMPT = (
    "OCR transcription only. Read the Korean math problem in the image and "
    "transcribe all visible Korean text, mathematical expressions, question "
    "numbers, and answer choices in their original reading order. Keep Korean in "
    "Korean; do not translate. Write mathematical expressions in LaTeX enclosed by "
    "\\( and \\). Do not solve, simplify, correct, explain, or add missing content. "
    "Preserve every number, sign, exponent, fraction, and choice label. If a part "
    "is unreadable, write [UNREADABLE] at that location. Output only the "
    "transcription."
)


def _read(client, image, prompt, region, model, max_tokens=1024):
    yield {"type": "region_start", "region_id": region.id, "model": model, "kind": region.kind}
    result = {"text": "", "finish_reason": "error", "issues": []}
    try:
        with closing(client.stream(image, prompt, max_tokens)) as events:
            for event in events:
                if event["type"] == "delta":
                    result["text"] += event["text"]
                    yield {**event, "region_id": region.id, "model": model}
                elif event["type"] == "complete":
                    result.update({k: v for k, v in event.items() if k != "type"})
    except OCRError as error:
        result["issues"].append(str(error))
    if result["finish_reason"] != "stop":
        result["issues"].append("판독이 중단됐습니다: " + result["finish_reason"])
    result["issues"] += text_issues(result["text"])
    result["usable"] = result["finish_reason"] == "stop" and bool(result["text"].strip())
    yield {"type": "region_complete", "region_id": region.id, "model": model, **result}
    return result


def _figure_data(text: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    data = json.loads(cleaned)
    keys = {"observations", "targets", "unreadable"}
    if not isinstance(data, dict) or set(data) != keys:
        raise ValueError("도형 판독의 필수 필드가 없습니다.")
    if any(not isinstance(data[k], list) or len(data[k]) > 32
           or any(not isinstance(v, str) or not v.strip() for v in data[k]) for k in keys):
        raise ValueError("도형 판독 항목이 올바르지 않습니다.")
    if not data["observations"]:
        raise ValueError("도형에서 관찰한 정보가 없습니다.")
    return data


class OCRPipeline:
    def __init__(
        self,
        paddle: Backend,
        qwen: Backend | None = None,
        formula=None,
        *,
        primary_engine="paddle",
        rotation=0,
        orientation_detector=None,
    ):
        self.paddle, self.qwen = paddle, qwen
        self.formula = formula
        if primary_engine not in {"paddle", "qwen"}:
            raise ValueError("primary_engine must be paddle or qwen")
        if rotation not in {0, 90, 180, 270}:
            raise ValueError("rotation must be 0, 90, 180 or 270")
        self.primary_engine = primary_engine
        self.rotation = rotation
        self.orientation_detector = orientation_detector

    def recognize(self, image: bytes, regions: list[Region] | None = None, *, check_text: bool = False) -> dict:
        """최종 결과만 반환한다. 증분 출력은 stream()을 사용한다."""
        for event in self.stream(image, regions, check_text=check_text):
            if event["type"] == "result":
                return event["result"]
        raise OCRError("OCR 결과를 받지 못했습니다.")

    def stream(self, image: bytes, regions: list[Region] | None = None, *, check_text: bool = False):
        """stage/delta/region_complete/result 이벤트. delta는 검수 전의 임시 문자열이다.

        영역을 생략하면 전체를 본문으로 읽는다. 자동 도형·표 탐지는 수행하지 않는다.
        formula 영역은 문장이 섞이지 않은 수식에만 지정하며 후보를 본문에 덮어쓰지 않는다.
        영역 좌표는 EXIF 보정과 추가 반시계 회전까지 적용된 이미지 기준이다.
        """
        if check_text and self.qwen is None:
            raise ValueError("보조 판독에는 Qwen 모델 또는 서버 설정이 필요합니다.")
        source = open_image(image)
        orientation = None
        if self.orientation_detector is not None:
            if regions is not None or self.rotation:
                orientation = {"status": "skipped", "rotation_ccw": 0, "reason": "explicit_coordinates_or_rotation"}
            else:
                yield {"type": "stage", "stage": "orientation"}
                orientation = self.orientation_detector.detect(source)
                angle = orientation.get("rotation_ccw", 0)
                if angle not in {0, 90, 180, 270}:
                    raise ValueError("자동 방향 보정 각도가 올바르지 않습니다.")
                if angle:
                    source = source.rotate(angle, expand=True)
                yield {"type": "orientation", **orientation}
        if self.rotation:
            source = source.rotate(self.rotation, expand=True)
        selected = list(regions) if regions is not None else [Region("problem")]
        if not selected or len(selected) > 32:
            raise ValueError("영역은 서로 다른 ID로 1~32개 지정해 주세요.")
        for region in selected:
            region_box(source, region)
        if len({r.id for r in selected}) != len(selected):
            raise ValueError("영역 ID가 중복됐습니다.")
        issues = []
        if orientation is not None and orientation["status"] in {"uncertain", "unavailable"}:
            issues.append("사진 방향을 확실하게 판단하지 못했습니다. 원본 방향과 인식 내용을 확인해 주세요.")
        if regions is None:
            issues.append("전체 이미지를 본문으로 읽습니다. 표·도형은 별도 영역 지정과 확인이 필요합니다.")
        outside = uncovered_content(source, selected)
        if outside > max(25, source.width * source.height * 0.0002):
            issues.append("선택 영역 밖에 내용이 남아 있습니다. 잘린 글자·선택지·도형을 확인해 주세요.")
        prepared, results = {}, {}
        for region in selected:
            prepared[region.id], downscaled = prepare(source, region)
            if downscaled:
                issues.append(f"{region.id}: 큰 이미지를 축소했습니다. 작은 기호를 확인해 주세요.")
            results[region.id] = {"id": region.id, "kind": region.kind, "bbox": region.bbox,
                                  "text": "", "finish_reason": "not_processed", "usable": False, "issues": []}

        # 주 OCR 단계를 끝낸 뒤 보조 판독을 실행한다.
        paddle_regions = [r for r in selected if r.kind != "figure" and not (self.formula is not None and r.kind == "formula")]
        if paddle_regions:
            yield {"type": "stage", "stage": self.primary_engine}
            try:
                with self.paddle.connect() as client:
                    for region in paddle_regions:
                        prompt = (
                            PROBLEM_PROMPT
                            if self.primary_engine == "qwen" and region.kind == "text"
                            else PADDLE_TASKS[region.kind]
                        )
                        record = yield from _read(
                            client, prepared[region.id], prompt,
                            region, self.primary_engine,
                        )
                        results[region.id].update(record)
                        if region.kind == "formula":
                            record["issues"].append("수식 재인식 후보입니다. 확인 없이 본문을 대체하지 마세요.")
                        if region.kind == "table" and record["usable"]:
                            try:
                                results[region.id]["table"] = parse_table(record["text"])
                            except ValueError as error:
                                results[region.id]["usable"] = False
                                record["issues"].append(str(error))
                        if record["finish_reason"] == "error":
                            break
            except (OCRError, OSError) as error:
                issues.append(str(error))

        formula_regions = (
            [r for r in selected if r.kind == "formula"]
            if self.formula is not None
            else []
        )

        if formula_regions:
            yield {"type": "stage", "stage": "formula"}

            try:
                with self.formula.connect() as client:
                    for region in formula_regions:
                        record = yield from _read(
                            client,
                            prepared[region.id],
                            PADDLE_TASKS["formula"],
                            region,
                            "pp-formulanet-plus-m",
                        )

                        record["issues"].append(
                            "수식 재인식 후보입니다. "
                            "확인 없이 본문을 대체하지 마세요."
                        )

                        results[region.id].update(record)

            except (OCRError, OSError) as error:
                issues.append(str(error))

        text = "\n\n".join(results[r.id]["text"] for r in selected
                             if r.kind == "text" and results[r.id]["usable"])
        context = re.split(r"①|\\textcircled\s*\{1\}", text, maxsplit=1)[0]
        if len(context) > 4000:
            issues.append("도형 판독의 참고 본문을 일부 생략했습니다.")
        qwen_regions = [r for r in selected if r.kind == "figure" or (check_text and r.kind == "text")]
        if qwen_regions and self.qwen is None:
            issues.append("도형 보조 판독용 Qwen이 설정되지 않았습니다.")
        elif qwen_regions:
            yield {"type": "stage", "stage": "qwen"}
            try:
                with self.qwen.connect() as client:
                    for region in qwen_regions:
                        prompt = TEXT_PROMPT
                        if region.kind == "figure":
                            prompt = (
                                "참고 본문(OCR 오류 가능):\n" + context[:4000] +
                                '\n그림에서 직접 보이는 조건과 연결 관계만 추출하세요. 계산하지 마세요. '
                                '본문 수치를 그림에서 본 것처럼 쓰지 마세요. JSON만 출력하세요: '
                                '{"observations":["직접 읽은 조건"],"targets":["문제에서 구할 미지수"],'
                                '"unreadable":["이미지에서 읽지 못한 부분"]}. '
                                '구해야 할 답은 unreadable에 넣지 마세요. 각 목록은 짧게 쓰세요.'
                            )
                        alternate = yield from _read(client, prepared[region.id], prompt, region, "qwen", 512)
                        if region.kind == "figure":
                            results[region.id].update(alternate)
                            results[region.id]["issues"].append("도형의 수치·위치 관계는 원본과 확인해야 합니다.")
                            if alternate["usable"]:
                                try:
                                    results[region.id]["figure"] = _figure_data(alternate["text"])
                                except ValueError as error:
                                    results[region.id]["usable"] = False
                                    results[region.id]["issues"].append(str(error))
                        else:
                            results[region.id]["alternative"] = alternate
                            if alternate["usable"] and results[region.id]["usable"]:
                                results[region.id]["issues"] += compare_readings(results[region.id]["text"], alternate["text"])
                        if alternate["finish_reason"] == "error":
                            break
            except (OCRError, OSError) as error:
                issues.append(str(error))

        usable = sum(r["usable"] for r in results.values())
        status = "failed" if not usable else "incomplete" if usable < len(results) else "needs_review"
        yield {"type": "result", "result": {"status": status, "text": text,
               "image_size": list(source.size),
               "rotation_ccw": (self.rotation + (orientation or {}).get("rotation_ccw", 0)) % 360,
               "orientation": orientation,
               "uncovered_dark_pixels": outside,
               "issues": issues, "regions": list(results.values())}}
