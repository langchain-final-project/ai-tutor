"""로컬 llama.cpp 기반 문제 OCR. 모델 실행 및 API 연결은 호출자가 선택한다."""

from .images import Region
from .llama import Backend, OCRError
from .pipeline import OCRPipeline

__all__ = ["Backend", "OCRError", "OCRPipeline", "Region"]
