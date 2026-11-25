"""PP-FormulaNet_plus-M 수식 인식 연결."""

from contextlib import contextmanager
from functools import lru_cache
import json
import os
from pathlib import Path
from threading import Lock
from time import perf_counter

from .llama import OCRError


class FormulaBackend:
    model_name = "PP-FormulaNet_plus-M"

    def __init__(self, *, device="cpu", model_dir=None):
        self.device = device
        self.model_dir = model_dir
        self._model = None
        self._lock = Lock()
        self.load_seconds = None

    @contextmanager
    def connect(self):
        with self._lock:
            if self._model is None:
                started = perf_counter()

                try:
                    if self.model_dir is not None and any(
                        not (Path(self.model_dir) / name).is_file()
                        for name in ("inference.json", "inference.yml", "inference.pdiparams")
                    ):
                        raise FileNotFoundError("setup_formula_model.py로 수식 모델을 먼저 준비해 주세요.")
                    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
                    from paddleocr import FormulaRecognition

                    options = {
                        "model_name": self.model_name,
                        "device": self.device,
                        "enable_mkldnn": False,
                        "cpu_threads": 2,
                    }

                    if self.model_dir is not None:
                        options["model_dir"] = str(self.model_dir)

                    self._model = FormulaRecognition(**options)

                except Exception as error:
                    raise OCRError(
                        "수식 모델을 불러오지 못했습니다: " + str(error), retryable=False,
                    ) from error

                self.load_seconds = round(
                    perf_counter() - started, 3
                )

        yield self

    def stream(self, image, prompt, max_tokens=1024):
        started = perf_counter()

        try:
            import cv2
            import numpy as np

            array = cv2.imdecode(
                np.frombuffer(image, dtype=np.uint8),
                cv2.IMREAD_COLOR,
            )

            if array is None:
                raise ValueError("수식 이미지가 올바르지 않습니다.")

            with self._lock:
                if self._model is None:
                    raise ValueError("connect() 안에서 호출해 주세요.")

                predictions = list(
                    self._model.predict(input=array, batch_size=1)
                )

            if len(predictions) != 1:
                raise ValueError(
                    "수식 한 영역에 대한 결과 한 개가 필요합니다."
                )

            data = predictions[0].json

            if isinstance(data, str):
                data = json.loads(data)

            if not isinstance(data, dict):
                raise ValueError("수식 모델 응답이 올바르지 않습니다.")

            record = data.get("res", data)
            text = (
                record.get("rec_formula")
                if isinstance(record, dict)
                else None
            )

            if not isinstance(text, str) or not text.strip():
                raise ValueError("인식된 수식이 없습니다.")

        except Exception as error:
            raise OCRError(
                "수식 인식에 실패했습니다: " + str(error)
            ) from error

        yield {
            "type": "complete",
            "text": text,
            "finish_reason": "stop",
            "seconds": round(perf_counter() - started, 3),
        }


@lru_cache(maxsize=1)
def get_formula_backend():
    root = Path(__file__).resolve().parents[3]
    return FormulaBackend(model_dir=os.getenv(
        "DIDGO_FORMULA_MODEL_DIR",
        str(root / ".venv/formula-model-cache/official_models/PP-FormulaNet_plus-M"),
    ))
