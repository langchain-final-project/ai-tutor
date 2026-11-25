"""방향 보정이 끝난 문제 이미지의 영역을 검출한다."""

from functools import lru_cache
import json
import logging
import os
from pathlib import Path
from threading import Lock

from app.ocr.layout_conversion import convert_detections


logger = logging.getLogger(__name__)

MODEL_NAME = "PP-DocLayout_plus-L"


class DocumentLayoutDetector:
    def __init__(self, model_dir):
        self.model_dir = Path(model_dir)
        self._model = None
        self._lock = Lock()

    def _load(self):
        if self._model is None:
            required_files = (
                "inference.json",
                "inference.yml",
                "inference.pdiparams",
            )

            if any(
                not (self.model_dir / name).is_file()
                for name in required_files
            ):
                raise FileNotFoundError(
                    "영역 검출 모델 파일을 확인해 주세요."
                )

            os.environ.setdefault(
                "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK",
                "True",
            )

            from paddleocr import LayoutDetection

            self._model = LayoutDetection(
                model_name=MODEL_NAME,
                model_dir=str(self.model_dir),
                device="cpu",
                enable_mkldnn=False,
                cpu_threads=2,
                threshold=0.3,
                layout_merge_bboxes_mode="union",
            )

        return self._model

    def detect(self, image):
        """선택 범위 자르기·방향 보정 후의 PIL 이미지를 받는다."""
        try:
            import numpy as np

            # Paddle 검출기의 배열 입력은 BGR 순서입니다.
            with image.convert("RGB") as rgb:
                array = np.asarray(rgb)[:, :, ::-1].copy()

            # 모델 로딩과 실행을 보호하고 같은 모델을 재사용합니다.
            with self._lock:
                model = self._load()
                results = list(
                    model.predict(
                        input=array,
                        batch_size=1,
                    )
                )

            if len(results) != 1:
                raise ValueError(
                    "이미지 한 장의 영역 검출 결과가 필요합니다."
                )

            raw = results[0].json

            if isinstance(raw, str):
                raw = json.loads(raw)

            layout, report = convert_detections(
                raw,
                image.size,
            )

            return {
                "model": MODEL_NAME,
                "status": (
                    "detected"
                    if report["raw_count"] > 0
                    else "not_detected"
                ),
                "layout": layout.model_dump(mode="json"),
                "report": report,
            }

        except Exception:
            logger.exception("문제 영역 검출에 실패했습니다.")

            return {
                "model": MODEL_NAME,
                "status": "unavailable",
                "layout": None,
                "report": None,
                "message": (
                    "문제 영역을 구분하지 못했어요. "
                    "원본 사진과 인식 내용을 확인해 주세요."
                ),
            }


@lru_cache(maxsize=1)
def get_layout_detector():
    root = Path(__file__).resolve().parents[3]

    model_dir = os.getenv(
        "DIDGO_LAYOUT_MODEL_DIR",
        str(
            root
            / ".venv/layout-model-cache/official_models"
            / MODEL_NAME
        ),
    )

    return DocumentLayoutDetector(model_dir)