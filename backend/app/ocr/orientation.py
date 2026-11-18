"""원본을 보존하면서 문서의 90도 단위 방향을 보수적으로 판별한다."""

from functools import lru_cache
import json
import logging
import math
import os
from pathlib import Path
from threading import Lock
from time import perf_counter

from PIL import Image

logger = logging.getLogger(__name__)
MODEL_NAME = "PP-LCNet_x1_0_doc_ori"
ANGLES = (0, 90, 180, 270)


def combine_predictions(predictions, *, threshold=0.8, min_margin=0.25):
    """4개 입력 방향의 예측을 같은 원본 좌표로 환산해 일관성을 검사한다.

    점수는 보정된 정확도 확률이 아니다. 임계값과 전원 일치 조건은 초기 운용 규칙이다.
    """
    if len(predictions) != 4:
        raise ValueError("방향 판별 결과가 4개여야 합니다.")

    totals = dict.fromkeys(ANGLES, 0.0)
    votes = []
    for clockwise, prediction in zip(ANGLES, predictions):
        labels, scores = prediction["label_names"], prediction["scores"]
        if len(labels) != 4 or len(scores) != 4:
            raise ValueError("방향별 점수가 누락됐습니다.")
        mapped = {}
        for label, value in zip(labels, scores):
            angle, score = int(label), float(value)
            if angle not in ANGLES or not math.isfinite(score) or not 0 <= score <= 1:
                raise ValueError("방향 판별 점수가 올바르지 않습니다.")
            mapped[(angle - clockwise) % 360] = score
        if len(mapped) != 4:
            raise ValueError("방향 판별 라벨이 중복됐습니다.")
        votes.append(max(mapped, key=mapped.get))
        for angle, score in mapped.items():
            totals[angle] += score / 4

    ranked = sorted(totals, key=totals.get, reverse=True)
    best = ranked[0]
    score, margin = totals[best], totals[best] - totals[ranked[1]]
    agree = votes.count(best)
    confident = score >= threshold and margin >= min_margin and agree == 4
    return {
        "model": MODEL_NAME,
        "status": ("unchanged" if best == 0 else "corrected") if confident else "uncertain",
        "rotation_ccw": best if confident else 0,
        "predicted_rotation_ccw": best,
        "score": round(score, 5),
        "margin": round(margin, 5),
        "votes": agree,
        "passes": 4,
        "threshold": threshold,
    }


class DocumentOrientationDetector:
    def __init__(self, model_dir, *, threshold=0.8, min_margin=0.25):
        self.model_dir = Path(model_dir)
        self.threshold = threshold
        self.min_margin = min_margin
        self._model = None
        self._lock = Lock()

    def _load(self):
        if self._model is None:
            if any(not (self.model_dir / name).is_file() for name in (
                "inference.json", "inference.yml", "inference.pdiparams",
            )):
                raise FileNotFoundError("방향 모델을 먼저 설치해 주세요.")
            os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
            from paddleocr import DocImgOrientationClassification
            self._model = DocImgOrientationClassification(
                model_name=MODEL_NAME,
                model_dir=str(self.model_dir),
                device="cpu",
                enable_mkldnn=False,
                cpu_threads=2,
                topk=4,
            )
        return self._model

    def detect(self, image):
        started = perf_counter()
        fallback = {"model": MODEL_NAME, "status": "uncertain", "rotation_ccw": 0, "score": None}
        if min(image.size) < 160:
            return {**fallback, "reason": "too_small"}
        with image.convert("L") as gray:
            low, high = gray.getextrema()
        if high - low < 20:
            return {**fallback, "reason": "low_contrast"}

        try:
            import numpy as np
            with image.copy() as small:
                small.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
                arrays = []
                for clockwise in ANGLES:
                    with small.rotate(-clockwise, expand=True) as rotated:
                        arrays.append(np.asarray(rotated.convert("RGB"))[:, :, ::-1].copy())

            with self._lock:
                model = self._load()
                records = list(model.predict(input=arrays, batch_size=4))

            predictions = []
            for record in records:
                data = record.json
                if isinstance(data, str):
                    data = json.loads(data)
                predictions.append(data.get("res", data))
            decision = combine_predictions(
                predictions, threshold=self.threshold, min_margin=self.min_margin,
            )
            return {**decision, "seconds": round(perf_counter() - started, 3)}
        except Exception:
            logger.exception("사진 방향을 판별하지 못했습니다.")
            return {**fallback, "status": "unavailable", "reason": "model_unavailable"}


@lru_cache(maxsize=1)
def get_orientation_detector():
    root = Path(__file__).resolve().parents[3]
    directory = os.getenv(
        "DIDGO_ORIENTATION_MODEL_DIR",
        str(root / ".venv/ocr-models" / MODEL_NAME),
    )
    return DocumentOrientationDetector(directory)
