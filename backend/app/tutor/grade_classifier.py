"""문제 내용에서 학년을 추정한다. 입력 학년은 특징으로 사용하지 않는다."""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

import numpy as np
import sklearn

from .classification_contract import ClassificationRequest, GradeCandidate, SUPPORTED_GRADES
from .type_classifier import (
    DEFAULT_MODELS_DIR, MODES, PREPROCESSOR_VERSION, encode_request, load_head,
)


GRADE_FORMAT_VERSION = 1
DEFAULT_GRADE_MODEL_DIR = DEFAULT_MODELS_DIR / "grade-v1"


@dataclass
class GradeEstimate:
    grade: int | None
    candidates: list[GradeCandidate]
    reason: str


class GradeClassifier:
    def __init__(self, heads, metadata):
        self.heads, self.metadata = heads, metadata

    @classmethod
    def load(cls, directory: Path = DEFAULT_GRADE_MODEL_DIR):
        directory = Path(directory)
        metadata = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        if (not isinstance(metadata, dict) or metadata.get("task") != "grade_prediction"
                or metadata.get("format_version") != GRADE_FORMAT_VERSION
                or metadata.get("preprocessor_version") != PREPROCESSOR_VERSION
                or metadata.get("grades") != list(SUPPORTED_GRADES)
                or metadata.get("sklearn_version") != sklearn.__version__):
            raise ValueError("학년 추정 모델의 형식·전처리·의존성 버전이 다릅니다.")
        if not isinstance(metadata.get("heads"), dict) or set(metadata["heads"]) != set(MODES):
            raise ValueError("학년 추정 모델의 본문·도형 구성에 오류가 있습니다.")
        heads = {}
        for mode in MODES:
            info = metadata["heads"][mode]
            threshold = info.get("margin_threshold")
            if threshold is not None and (
                isinstance(threshold, bool) or not isinstance(threshold, (int, float))
                or not np.isfinite(threshold) or threshold < 0
            ):
                raise ValueError("학년 추정의 점수 차이 기준이 올바르지 않습니다.")
            ambiguous = info.get("ambiguous_inputs")
            if not isinstance(ambiguous, list) or any(not isinstance(v, str) or len(v) != 64 for v in ambiguous):
                raise ValueError("학년 중복 입력 목록이 올바르지 않습니다.")
            heads[mode] = load_head(directory, mode, info, len(SUPPORTED_GRADES))
        return cls(heads, metadata)

    def infer(self, payload: ClassificationRequest | dict) -> GradeEstimate:
        request = ClassificationRequest.model_validate(payload)
        main, figure = encode_request(request)
        mode = "body_figure" if figure else "body"
        text = main + figure
        scores, nonzero = self.heads[mode].score_texts([text])
        if not nonzero[0]:
            return GradeEstimate(None, [], "no_features")
        order = np.argsort(-scores[0], kind="stable")
        candidates = [GradeCandidate(grade=SUPPORTED_GRADES[i], score=float(scores[0, i])) for i in order]
        info = self.metadata["heads"][mode]
        if hashlib.sha256(text.encode("utf-8")).hexdigest() in info["ambiguous_inputs"]:
            return GradeEstimate(None, candidates, "ambiguous_input")
        threshold = info["margin_threshold"]
        margin = candidates[0].score - candidates[1].score
        if threshold is None or margin <= 0 or margin < threshold:
            return GradeEstimate(None, candidates, "low_margin")
        return GradeEstimate(candidates[0].grade, candidates, "accepted")
