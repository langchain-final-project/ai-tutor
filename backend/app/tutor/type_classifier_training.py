"""공유 문제은행을 읽기 전용으로 학습한다. 평가 문항·숫자만 다른 동일 틀은 제외한다."""

from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import warnings

import numpy as np
import sklearn
from sklearn.exceptions import ConvergenceWarning
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.svm import LinearSVC
from threadpoolctl import threadpool_limits

from .classification_contract import TypeCandidate
from .type_classifier import (
    DEFAULT_MODELS_DIR, FORMAT_VERSION, MODES, PREPROCESSOR_VERSION, ROOT,
    SUPPORTED_GRADES, VECTORIZER_OPTIONS, TypeClassifier, file_sha256, model_directory, normalize_input,
)


DEFAULT_DATABASE = ROOT / "data/problem_bank/problem_bank.seed.sqlite3"


def load_training_corpus(database: Path, *, grade: int = 1) -> tuple[list[dict], dict]:
    model_directory(grade)  # 학년 인수 검증. 파일을 생성하지 않는다.
    with closing(sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        rows = [dict(row) for row in connection.execute(
            """SELECT q.id, q.grade, q.question_text, q.figure_text, q.source_topic_code,
                      t.source_topic_name FROM questions q JOIN topics t USING(source_topic_code)
               WHERE q.grade=? ORDER BY q.id""", (grade,),
        )]
        reserved = {row[0] for row in connection.execute(
            """SELECT b.question_id FROM benchmark_items b
               JOIN questions q ON q.id=b.question_id WHERE q.grade=?""", (grade,),
        )}
    if not rows:
        raise ValueError(f"문제은행에 중{grade} 학습 문항이 없습니다.")

    def template(text):
        return re.sub(r"\d+(?:\.\d+)?", "NUM", normalize_input(text))

    held = {template(row["question_text"]) for row in rows if row["id"] in reserved}
    selected = [row for row in rows if row["id"] not in reserved and template(row["question_text"]) not in held]
    if len({row["source_topic_code"] for row in selected}) < 2:
        raise ValueError("평가 문항을 제외한 학습 데이터에 유형이 2개 이상 필요합니다.")
    canonical = json.dumps(selected, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    summary = {
        "grade": grade, "source_questions": len(rows), "training_questions": len(selected),
        "reserved_questions": len(reserved), "excluded_questions": len(rows) - len(selected),
        "training_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }
    return selected, summary


def train_classifier(database: Path = DEFAULT_DATABASE, output_dir: Path | None = None, *, grade: int = 1, progress=None) -> dict:
    default_directory = model_directory(grade)
    output_dir = Path(output_dir) if output_dir is not None else default_directory
    if output_dir.exists():
        raise ValueError("기존 모델 폴더는 덮어쓰지 않습니다. 다른 --output-dir를 지정해 주세요.")
    rows, summary = load_training_corpus(database, grade=grade)
    labels = np.array([row["source_topic_code"] for row in rows])
    counts = Counter(labels)
    names = {row["source_topic_code"]: row["source_topic_name"] for row in rows}
    classes = [{"code": code, "name": names[code], "training_count": counts[code]} for code in sorted(names)]
    for item in classes:
        TypeCandidate(source_topic_code=item["code"], source_topic_name=item["name"], reasons=["학습 유형 검증"])
    heads = {}
    with threadpool_limits(limits=4):
        for mode in MODES:
            if progress:
                progress(mode)
            texts = [normalize_input(row["question_text"] + (
                " " + (row["figure_text"] or "") if mode == "body_figure" else "")) for row in rows]
            vectorizer = TfidfVectorizer(**VECTORIZER_OPTIONS, dtype=np.float32)
            features = vectorizer.fit_transform(texts)
            model = LinearSVC(C=10, class_weight="balanced", dual="auto", max_iter=4000, random_state=42)
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always", ConvergenceWarning)
                model.fit(features, labels)
            if any(issubclass(w.category, ConvergenceWarning) for w in caught):
                raise ValueError("モデルが収束しませんでした。未収束のモデルは保存しません。")
            if model.classes_.tolist() != [item["code"] for item in classes]:
                raise ValueError("モデルとタイプ一覧の順序が一致しません。")
            coefficients, intercept = model.coef_, model.intercept_
            if coefficients.shape[0] == 1:
                # 二分類の決定値を各クラスの -margin/+margin にそろえる。
                coefficients = np.vstack([-coefficients, coefficients])
                intercept = np.concatenate([-intercept, intercept])
            heads[mode] = {
                "coefficients": coefficients, "intercept": intercept,
                "idf": vectorizer.idf_, "vocabulary": vectorizer.get_feature_names_out().tolist(),
            }

    # 全モデルの学習成功後に新規フォルダへ保存。manifestは最後に書く。
    # 保存途中で失敗したフォルダは読み込めず、再学習には別フォルダを指定する。
    output_dir.mkdir(parents=True, exist_ok=False)
    metadata = {
        "format_version": FORMAT_VERSION, "preprocessor_version": PREPROCESSOR_VERSION,
        "algorithm": "TF-IDF character 2-5gram + LinearSVC C=10 balanced",
        "sklearn_version": sklearn.__version__, "numpy_version": np.__version__,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "supported_grades": [grade], "corpus": summary,
        "classes": classes, "heads": {},
    }
    for mode, head in heads.items():
        path = output_dir / f"{mode}.npz"
        with path.open("xb") as stream:
            np.savez_compressed(stream, coefficients=head["coefficients"],
                                intercept=head["intercept"], idf=head["idf"])
        metadata["heads"][mode] = {"sha256": file_sha256(path), "vocabulary": head["vocabulary"]}
    with (output_dir / "manifest.json").open("x", encoding="utf-8") as stream:
        json.dump(metadata, stream, ensure_ascii=False, indent=2)
    return {
        "model_dir": str(output_dir.resolve()), **summary, "types": len(classes),
        "feature_counts": {mode: len(head["vocabulary"]) for mode, head in heads.items()},
    }


def train_school_classifiers(
    database: Path = DEFAULT_DATABASE, model_root: Path = DEFAULT_MODELS_DIR,
    *, grades: tuple[int, ...] = SUPPORTED_GRADES, progress=None,
) -> dict:
    """학년별로 학습한다. 이미 있는 모델은 데이터·학년·파일을 검증한 후 재사용한다."""
    grades = tuple(grades)
    if not grades or len(set(grades)) != len(grades):
        raise ValueError("학년은 중복 없이 1개 이상 지정해 주세요.")
    for grade in grades:
        model_directory(grade, model_root)
    results = []
    for grade in grades:
        directory = model_directory(grade, model_root)
        if directory.exists():
            if progress:
                progress(grade, "reuse")
            model = TypeClassifier.load(directory)
            _, summary = load_training_corpus(database, grade=grade)
            if model.grade != grade or model.metadata["corpus"]["training_sha256"] != summary["training_sha256"]:
                raise ValueError(f"기존 중{grade} 모델과 현재 학습 데이터가 다릅니다. 다른 --model-root를 사용해 주세요.")
            results.append({"model_dir": str(directory.resolve()), "status": "reused", **summary,
                            "types": len(model.classes)})
            del model
        else:
            report = train_classifier(database, directory, grade=grade,
                                      progress=(lambda mode, g=grade: progress(g, mode)) if progress else None)
            results.append({"status": "trained", **report})
    report = {"model_root": str(Path(model_root).resolve()), "grades": results,
              "total_training_questions": sum(r["training_questions"] for r in results),
              "total_types": sum(r["types"] for r in results)}
    if set(grades) == set(SUPPORTED_GRADES):
        from .grade_classifier_training import train_grade_classifier

        report["grade_model"] = train_grade_classifier(
            database, Path(model_root) / "grade-v1",
            progress=(lambda mode: progress(0, mode)) if progress else None,
        )
    return report
