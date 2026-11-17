"""학년 모델과 추가 확인 기준을 학습한다. 원천 Validation은 튜닝에 사용하지 않는다."""

from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import warnings

import numpy as np
import sklearn
from sklearn.exceptions import ConvergenceWarning
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.model_selection import GroupShuffleSplit
from sklearn.svm import LinearSVC
from threadpoolctl import threadpool_limits

from .classification_contract import SUPPORTED_GRADES
from .grade_classifier import DEFAULT_GRADE_MODEL_DIR, GRADE_FORMAT_VERSION, GradeClassifier
from .type_classifier import MODES, PREPROCESSOR_VERSION, VECTORIZER_OPTIONS, file_sha256, normalize_input
from .type_classifier_training import DEFAULT_DATABASE, load_training_corpus


def calibration_threshold(scores, truth, *, target_accuracy=0.95) -> tuple[float | None, dict]:
    """점수 차이 내림차순에서 목표 정확도를 만족하는 최대 범위를 고른다. 점수는 확률이 아니다."""
    order = np.argsort(-scores, axis=1, kind="stable")
    predicted = np.array(SUPPORTED_GRADES)[order[:, 0]]
    margins = scores[np.arange(len(scores)), order[:, 0]] - scores[np.arange(len(scores)), order[:, 1]]
    ranked = np.argsort(-margins, kind="stable")
    correct = predicted == truth
    prefix_correct = np.cumsum(correct[ranked])
    counts = np.arange(1, len(scores) + 1)
    # 같은 margin의 일부만 골라 기준이 좋아 보이는 것을 막는다.
    boundary = np.r_[margins[ranked][:-1] != margins[ranked][1:], True]
    minimum = max(20, math.ceil(len(scores) * 0.05))
    valid = np.flatnonzero(boundary & (counts >= minimum)
                           & (prefix_correct / counts >= target_accuracy) & (margins[ranked] > 0))
    threshold = float(margins[ranked[valid[-1]]]) if len(valid) else None
    accepted = margins >= threshold if threshold is not None else np.zeros(len(scores), dtype=bool)
    return threshold, {
        "target_accuracy": target_accuracy, "questions": len(scores),
        "minimum_accepted": minimum, "accepted": int(accepted.sum()),
        "coverage": float(np.mean(accepted)),
        "accuracy_when_accepted": float(np.mean(correct[accepted])) if accepted.any() else None,
        "all_top1_accuracy": float(np.mean(correct)),
    }


def grade_training_data(database: Path):
    rows, fingerprints = [], {}
    for grade in SUPPORTED_GRADES:
        selected, summary = load_training_corpus(database, grade=grade)
        rows.extend(selected)
        fingerprints[str(grade)] = summary["training_sha256"]
    signature = hashlib.sha256(json.dumps(fingerprints, sort_keys=True).encode()).hexdigest()
    return rows, signature


def train_grade_classifier(database: Path = DEFAULT_DATABASE, output_dir: Path = DEFAULT_GRADE_MODEL_DIR, *, progress=None):
    rows, signature = grade_training_data(database)
    output_dir = Path(output_dir)
    if output_dir.exists():
        model = GradeClassifier.load(output_dir)
        if model.metadata["corpus_sha256"] != signature:
            raise ValueError("학년 모델의 학습 데이터가 다릅니다. 다른 --model-root를 사용해 주세요.")
        return {"status": "reused", "model_dir": str(output_dir.resolve()),
                "calibration": {mode: model.metadata["heads"][mode]["calibration"] for mode in MODES}}
    labels = np.array([row["grade"] for row in rows])
    # 숫자만 다른 본문, 같은 본문에 그림만 다른 문항을 양쪽에 나누지 않는다.
    groups = np.array([re.sub(r"\d+(?:\.\d+)?", "NUM", normalize_input(row["question_text"])) for row in rows])
    group_count = len(set(groups))
    if group_count < 6:
        raise ValueError("학년 학습·검증에 사용할 서로 다른 문제 틀이 부족합니다.")
    validation_groups = max(3, math.ceil(group_count * 0.2))
    splitter = GroupShuffleSplit(n_splits=64, test_size=validation_groups, random_state=42)
    for train_indices, calibration_indices in splitter.split(np.zeros(len(rows)), labels, groups):
        if (set(labels[train_indices]) == set(SUPPORTED_GRADES)
                and set(labels[calibration_indices]) == set(SUPPORTED_GRADES)):
            break
    else:
        raise ValueError("학년별로 분리된 학습·검증 자료를 구성할 수 없습니다.")
    assert not set(groups[train_indices]) & set(groups[calibration_indices])
    heads = {}
    with threadpool_limits(limits=4):
        for mode in MODES:
            if progress:
                progress(mode)
            texts = [normalize_input(r["question_text"] + (
                " " + (r["figure_text"] or "") if mode == "body_figure" else "")) for r in rows]
            vectorizer = TfidfVectorizer(**VECTORIZER_OPTIONS, dtype=np.float32)
            x_train = vectorizer.fit_transform([texts[i] for i in train_indices])
            x_calibration = vectorizer.transform([texts[i] for i in calibration_indices])
            model = LinearSVC(C=10, class_weight="balanced", dual="auto", max_iter=4000, random_state=42)
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always", ConvergenceWarning)
                model.fit(x_train, labels[train_indices])
            if any(issubclass(w.category, ConvergenceWarning) for w in caught):
                raise ValueError("학년 모델이 수렴하지 않아 저장하지 않았습니다.")
            if model.classes_.tolist() != list(SUPPORTED_GRADES):
                raise ValueError("학년 모델의 분류 순서가 올바르지 않습니다.")
            threshold, calibration = calibration_threshold(model.decision_function(x_calibration), labels[calibration_indices])
            observed_grades = defaultdict(set)
            for text, label in zip(texts, labels):
                observed_grades[text].add(int(label))
            ambiguous = sorted(hashlib.sha256(text.encode("utf-8")).hexdigest()
                               for text, grades in observed_grades.items() if len(grades) > 1)
            heads[mode] = {"coefficients": model.coef_, "intercept": model.intercept_, "idf": vectorizer.idf_,
                           "vocabulary": vectorizer.get_feature_names_out().tolist(),
                           "margin_threshold": threshold, "calibration": calibration, "ambiguous_inputs": ambiguous}
    output_dir.mkdir(parents=True, exist_ok=False)
    metadata = {
        "task": "grade_prediction", "format_version": GRADE_FORMAT_VERSION,
        "preprocessor_version": PREPROCESSOR_VERSION, "sklearn_version": sklearn.__version__,
        "grades": list(SUPPORTED_GRADES), "created_at": datetime.now(timezone.utc).isoformat(),
        "corpus_sha256": signature, "source_questions": len(rows),
        "training_questions": len(train_indices), "calibration_questions": len(calibration_indices),
        "split": "numeric-body-group-80/20, random_state=42, no official Validation tuning", "heads": {},
    }
    for mode, head in heads.items():
        path = output_dir / f"{mode}.npz"
        with path.open("xb") as stream:
            np.savez_compressed(stream, coefficients=head["coefficients"], intercept=head["intercept"], idf=head["idf"])
        metadata["heads"][mode] = {key: head[key] for key in ["vocabulary", "margin_threshold", "calibration", "ambiguous_inputs"]}
        metadata["heads"][mode]["sha256"] = file_sha256(path)
    with (output_dir / "manifest.json").open("x", encoding="utf-8") as stream:
        json.dump(metadata, stream, ensure_ascii=False, indent=2)
    return {"status": "trained", "model_dir": str(output_dir.resolve()),
            "training_questions": len(train_indices), "calibration_questions": len(calibration_indices),
            "calibration": {mode: head["calibration"] for mode, head in heads.items()}}
