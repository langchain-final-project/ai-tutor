"""학년별 TF-IDF/LinearSVC를 선택해 중학교 유형 후보를 반환한다. 최종 확정은 하지 않는다."""

from dataclasses import dataclass
import hashlib
import html
import json
from pathlib import Path
import re

import numpy as np
import sklearn
from sklearn.feature_extraction.text import TfidfVectorizer

from .classification_contract import (
    ClassificationRequest, CandidatesResult, ClarificationQuestion, ClarificationResult,
    SUPPORTED_GRADES, TypeCandidate, UnsupportedResult, validate_decision,
)


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MODELS_DIR = ROOT / "data/problem_bank/type_models"
# 단일 학년 로더의 이전 기본 경로는 보존한다. 전체 학년은 MiddleSchoolTypeClassifier를 사용한다.
DEFAULT_MODEL_DIR = DEFAULT_MODELS_DIR / "middle1-v1"
MODES = ("body", "body_figure")
FORMAT_VERSION = 1
PREPROCESSOR_VERSION = "char-tfidf-v1"
VECTORIZER_OPTIONS = {
    "analyzer": "char", "ngram_range": (2, 5), "min_df": 2, "max_features": 60000,
    "sublinear_tf": True, "lowercase": False,
}


def model_directory(grade: int, root: Path = DEFAULT_MODELS_DIR) -> Path:
    if isinstance(grade, bool) or not isinstance(grade, int) or grade not in SUPPORTED_GRADES:
        raise ValueError("모델 학년은 1, 2, 3 중 하나여야 합니다.")
    return Path(root) / f"middle{grade}-v1"


def _check_top_k(top_k: int) -> None:
    if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 5:
        raise ValueError("후보 수는 1~5 사이의 정수여야 합니다.")


def _pending_input(request: ClassificationRequest, *, include_grade: bool = True):
    missing = request.blocking_fields()
    if not include_grade:
        missing = [field for field in missing if field != "grade"]
    if not missing:
        return None
    questions = {
        "grade": "문제의 학년(중1·중2·중3)을 확인해 주세요.",
        "confirmation": "인식된 본문·수식·조건을 확인해 주세요.",
        "question_text": "문제 본문과 요구하는 내용을 입력해 주세요.",
        "equations": "문제의 수식을 확인해 주세요.",
        "conditions": "문제에 주어진 조건을 확인해 주세요.",
        "figures": "도형 존재 여부와 표시된 수치·위치 관계를 확인해 주세요.",
        "tables": "표 존재 여부와 행·열·빈칸을 확인해 주세요.",
        "targets": "문제에서 무엇을 구하라고 하는지 입력해 주세요.",
    }
    return validate_decision(request, ClarificationResult(
        reasons=["입력 확인 또는 필요한 정보 보완이 먼저 필요합니다."],
        questions=[ClarificationQuestion(field=field, question=questions[field]) for field in missing],
    ))


def normalize_input(text: str) -> str:
    """학습과 예측에 공통 적용한다. 원본 입력을 수정하거나 숫자를 가리지 않는다."""
    text = html.unescape(text).replace("−", "-").replace("‒", "-")
    text = re.sub(r"</?(?:table|tbody|thead|tr|td|th|caption|br|p|span)\b[^>]*>", " ", text, flags=re.I)
    return re.sub(r"\s+", "", text).replace("$", "")


def encode_request(request: ClassificationRequest) -> tuple[str, str]:
    """본문 밖에 분리된 수식·조건·표도 사용한다. 유형 이름·정답·학생 풀이는 넣지 않는다."""
    main = [request.question_text, *request.equations, *request.conditions, *request.targets]
    for table in request.tables:
        main.extend(" ".join(row) for row in table.rows)
    figures = []
    for figure in request.figures:
        figures.extend([figure.description, *figure.conditions])

    def combine(parts):
        # 완전히 같은 필드만 중복 제거한다. 'x=1'을 'x=10'의 일부라고 버리지 않는다.
        return "".join(dict.fromkeys(normalize_input(part) for part in parts if part.strip()))

    return combine(main), combine(figures)


def file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


@dataclass
class ModelHead:
    vectorizer: TfidfVectorizer
    coefficients: np.ndarray
    intercept: np.ndarray

    def score_texts(self, normalized_texts: list[str]) -> tuple[np.ndarray, np.ndarray]:
        features = self.vectorizer.transform(normalized_texts)
        scores = np.asarray(features @ self.coefficients.T) + self.intercept
        return scores, np.asarray(features.getnnz(axis=1)).ravel()


def load_head(directory: Path, mode: str, info: dict, class_count: int) -> ModelHead:
    """유형 모델과 학년 모델이 동일한 방식으로 숫자 계수·전처리 사전을 복원한다."""
    if not isinstance(info, dict):
        raise ValueError("모델별 전처리 정보가 올바르지 않습니다.")
    vocabulary = info.get("vocabulary")
    if (not isinstance(vocabulary, list) or not vocabulary
            or len(vocabulary) > VECTORIZER_OPTIONS["max_features"]
            or any(not isinstance(word, str) or not 2 <= len(word) <= 5 for word in vocabulary)
            or len(set(vocabulary)) != len(vocabulary)):
        raise ValueError("전처리 사전이 올바르지 않습니다.")
    path = Path(directory) / f"{mode}.npz"
    if file_sha256(path) != info["sha256"]:
        raise ValueError(f"모델 파일이 변경되거나 손상됐습니다: {mode}")
    with np.load(path, allow_pickle=False) as arrays:
        coefficients, intercept, idf = (arrays[key] for key in ("coefficients", "intercept", "idf"))
    if (coefficients.shape != (class_count, len(vocabulary))
            or intercept.shape != (class_count,) or idf.shape != (len(vocabulary),)
            or any(array.dtype.kind != "f" or not np.isfinite(array).all()
                   for array in (coefficients, intercept, idf)) or not (idf > 0).all()):
        raise ValueError("모델 계수·IDF·유형 수가 일치하지 않습니다.")
    vectorizer = TfidfVectorizer(**VECTORIZER_OPTIONS, dtype=np.float32,
                                vocabulary={word: i for i, word in enumerate(vocabulary)})
    vectorizer.idf_ = idf
    return ModelHead(vectorizer, coefficients, intercept)


class TypeClassifier:
    """manifest에 기록된 단일 학년만 분류한다. 다른 학년을 이 모델로 예측하지 않는다."""

    def __init__(self, classes: list[dict], heads: dict[str, ModelHead], metadata: dict):
        self.classes, self.heads, self.metadata = classes, heads, metadata
        self.grade = metadata["supported_grades"][0]

    @classmethod
    def load(cls, directory: Path = DEFAULT_MODEL_DIR) -> "TypeClassifier":
        """전처리 사전·IDF·모델 계수·유형 목록을 함께 검증하고 불러온다."""
        directory = Path(directory)
        metadata = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        if not isinstance(metadata, dict):
            raise ValueError("모델 manifest는 JSON 객체여야 합니다.")
        grades = metadata.get("supported_grades")
        if (metadata.get("format_version") != FORMAT_VERSION
                or metadata.get("preprocessor_version") != PREPROCESSOR_VERSION
                or not isinstance(grades, list) or len(grades) != 1
                or type(grades[0]) is not int or grades[0] not in SUPPORTED_GRADES):
            raise ValueError("모델 형식·전처리·지원 학년 버전이 다릅니다. 다시 학습해 주세요.")
        if metadata.get("sklearn_version") != sklearn.__version__:
            raise ValueError("학습 시 scikit-learn 버전과 다릅니다. 의존성을 맞추거나 다시 학습해 주세요.")
        classes = metadata.get("classes")
        if not isinstance(classes, list) or len(classes) < 2 or any(not isinstance(item, dict) for item in classes):
            raise ValueError("유형이 2개 이상인 모델이 필요합니다.")
        for item in classes:
            TypeCandidate(source_topic_code=item["code"], source_topic_name=item["name"],
                          reasons=["저장된 유형 목록 검증"])
        codes = [item["code"] for item in classes]
        if codes != sorted(set(codes)):
            raise ValueError("모델 유형 목록이 중복되거나 순서가 잘못됐습니다.")
        if not isinstance(metadata.get("heads"), dict) or set(metadata["heads"]) != set(MODES):
            raise ValueError("본문·도형 설명 모델의 구성이 올바르지 않습니다.")
        heads = {}
        for mode in MODES:
            heads[mode] = load_head(directory, mode, metadata["heads"][mode], len(classes))
        return cls(classes, heads, metadata)

    def predict(self, payload: ClassificationRequest | dict, *, top_k: int = 5):
        """확인된 입력에는 후보만 제시한다. 점수는 확률·확신도가 아닌 SVC 결정값이다."""
        _check_top_k(top_k)
        request = ClassificationRequest.model_validate(payload)
        if request.grade is not None and request.grade != self.grade:
            return validate_decision(request, UnsupportedResult(
                reasons=[f"이 모델은 중{self.grade} 전용입니다. 중{request.grade} 모델을 선택해 주세요."],
            ))
        pending = _pending_input(request)
        if pending is not None:
            return pending
        main, figure = encode_request(request)
        mode = "body_figure" if figure else "body"
        scores, nonzero = self.heads[mode].score_texts([main + figure])
        if not nonzero[0]:
            return validate_decision(request, ClarificationResult(
                reasons=["학습한 특징과 연결되는 내용이 없어 유형 후보를 만들지 않았습니다."],
                questions=[ClarificationQuestion(field="question_text", question="문제 본문·수식이 올바른지 확인해 주세요.")],
            ))
        order = np.argsort(-scores[0], kind="stable")[:min(top_k, len(self.classes))]
        candidates = [TypeCandidate(
            source_topic_code=self.classes[index]["code"], source_topic_name=self.classes[index]["name"],
            score=float(scores[0, index]), score_name="linear_svc_margin",
            reasons=[f"중{self.grade} {mode} 모델이 제안한 후보입니다. 최종 유형은 아직 확정하지 않았습니다."],
        ) for index in order]
        return validate_decision(request, CandidatesResult(
            candidates=candidates,
            grade=self.grade, grade_source="provided",
            reasons=["학습된 유형별 결정값 순서로 정렬했습니다. 동일 문항 검색과 최종 선택은 별도 단계입니다."],
        ))


class MiddleSchoolTypeClassifier:
    """학년을 생략하면 먼저 추정한다. 점수 차이가 불충분하면 학년만 추가 확인한다."""

    def __init__(self, model_root: Path = DEFAULT_MODELS_DIR):
        self.model_root = Path(model_root)
        self._models: dict[int, TypeClassifier] = {}
        self._grade_model = None

    def predict(self, payload: ClassificationRequest | dict, *, top_k: int = 5):
        _check_top_k(top_k)
        request = ClassificationRequest.model_validate(payload)
        pending = _pending_input(request, include_grade=request.grade is not None)
        if pending is not None:
            return pending
        grade = request.grade
        estimation = None
        if grade is None:
            from .grade_classifier import GradeClassifier

            directory = self.model_root / "grade-v1"
            if not (directory / "manifest.json").is_file():
                return validate_decision(request, ClarificationResult(
                    reasons=["학년 추정 모델이 없습니다. 전체 모델을 학습하거나 학년을 알려 주세요."],
                    questions=[ClarificationQuestion(field="grade", question="문제의 학년을 확인해 주세요.")],
                ))
            if self._grade_model is None:
                self._grade_model = GradeClassifier.load(directory)
            estimation = self._grade_model.infer(request)
            grade = estimation.grade
            if grade is None:
                return validate_decision(request, ClarificationResult(
                    reasons=["문제 내용만으로 학년을 충분히 구분하지 못했습니다."],
                    grade_candidates=estimation.candidates,
                    questions=[ClarificationQuestion(field="grade", question="이 문제의 학년(중1·중2·중3)을 확인해 주세요.")],
                ))
        if grade not in self._models:
            directory = model_directory(grade, self.model_root)
            if not (directory / "manifest.json").is_file():
                raise ValueError(f"중{grade} 모델이 없습니다. train_type_classifier.py --grades {grade}로 학습해 주세요.")
            model = TypeClassifier.load(directory)
            if model.grade != grade:
                raise ValueError(f"중{grade} 폴더에 다른 학년 모델이 있습니다.")
            self._models[grade] = model
        effective = ClassificationRequest.model_validate({
            **request.model_dump(), "grade": grade,
            "missing_information": [f for f in request.missing_information if f != "grade"],
        })
        result = self._models[grade].predict(effective, top_k=top_k)
        if estimation is not None:
            return validate_decision(request, {
                **result.model_dump(), "grade": grade, "grade_source": "predicted",
                "grade_candidates": [c.model_dump() for c in estimation.candidates],
            })
        return result
