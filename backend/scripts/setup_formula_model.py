"""공식 FormulaNet 모델을 프로젝트 캐시에 미리 준비한다."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.environ["PADDLE_PDX_CACHE_HOME"] = str(ROOT / ".venv/formula-model-cache")
os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"


if __name__ == "__main__":
    from paddleocr import FormulaRecognition

    model = FormulaRecognition(
        model_name="PP-FormulaNet_plus-M", device="cpu",
        enable_mkldnn=False, cpu_threads=2,
    )
    print("FormulaNet ready:", ROOT / ".venv/formula-model-cache/official_models/PP-FormulaNet_plus-M")
    model.close()
