"""공식 Validation을 튜닝에 쓰지 않고, 사전에 정한 분류기로 평가한다. OCR 평가는 아니다."""

import argparse
from collections import Counter
import html
import json
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = ROOT / "data/problem_bank/raw"


def load_split(path: Path, expected_split: str) -> list[dict]:
    with path.open(encoding="utf-8") as stream:
        rows = [json.loads(line) for line in stream if line.strip()]
    if not rows:
        raise ValueError(f"빈 데이터셋입니다: {path}")
    seen = set()
    for row in rows:
        if row["split"] != expected_split:
            raise ValueError(f"공식 split이 섞였습니다: {row['id']}")
        if row["id"] in seen:
            raise ValueError(f"문항 ID가 중복됐습니다: {row['id']}")
        seen.add(row["id"])
        if set(row["input"]) != {"question_text", "figure_text"}:
            raise ValueError("입력에는 본문과 제공된 그림 설명만 허용합니다.")
    return rows


def input_text(row: dict, mode: str) -> str:
    text = row["input"]["question_text"]
    if mode == "body_figure":
        text += " " + (row["input"]["figure_text"] or "")
    text = html.unescape(text).replace("−", "-").replace("‒", "-")
    text = re.sub(r"</?(?:table|tbody|thead|tr|td|th|caption|br|p|span)\b[^>]*>", " ", text, flags=re.I)
    return re.sub(r"\s+", "", text).replace("$", "")


def evaluate(train: list[dict], validation: list[dict], mode: str, output: Path) -> dict:
    # 선택 의존성은 실제 평가 실행 시에만 불러온다.
    import numpy as np
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics import f1_score
    from sklearn.svm import LinearSVC
    from threadpoolctl import threadpool_limits

    train_texts = [input_text(row, mode) for row in train]
    validation_texts = [input_text(row, mode) for row in validation]
    y_train = np.array([row["label"]["source_topic_code"] for row in train])
    y_validation = np.array([row["label"]["source_topic_code"] for row in validation])
    train_ids = {row["id"] for row in train}
    if train_ids & {row["id"] for row in validation}:
        raise ValueError("학습/평가 문항 ID가 겹칩니다.")
    started = time.perf_counter()
    vectorizer = TfidfVectorizer(
        analyzer="char", ngram_range=(2, 5), min_df=2, max_features=60000,
        sublinear_tf=True, lowercase=False, dtype=np.float32,
    )
    with threadpool_limits(limits=4):
        x_train = vectorizer.fit_transform(train_texts)
        x_validation = vectorizer.transform(validation_texts)
        # 이 설정은 이전 그룹 분할 실험에서 정했다. 공식 Validation으로 다시 튜닝하지 않는다.
        model = LinearSVC(C=10, class_weight="balanced", dual="auto", max_iter=4000, random_state=42)
        model.fit(x_train, y_train)
        score = model.decision_function(x_validation)
        order = np.argsort(-score, axis=1)
        top = model.classes_[order[:, :min(5, len(model.classes_))]]
        template_vectorizer = TfidfVectorizer(
            analyzer="char", ngram_range=(3, 5), min_df=2,
            max_features=50000, lowercase=False, dtype=np.float32,
        )
        template_train = template_vectorizer.fit_transform([
            re.sub(r"\d+(?:\.\d+)?", "NUM", text) for text in train_texts
        ])
        template_validation = template_vectorizer.transform([
            re.sub(r"\d+(?:\.\d+)?", "NUM", text) for text in validation_texts
        ])
        nearest_template = []
        for offset in range(0, len(validation), 128):
            similarities = template_validation[offset:offset + 128] @ template_train.T
            nearest_template.extend(np.asarray(similarities.max(axis=1).toarray()).ravel().tolist())
    nearest_template = np.clip(np.array(nearest_template), 0, 1)
    train_text_set = set(train_texts)
    exact_overlap = np.array([text in train_text_set for text in validation_texts])
    near_overlap = nearest_template >= 0.95
    unit_by_topic = {row["label"]["source_topic_code"]: row["label"]["unit_code"] for row in train}
    unit_truth = np.array([row["label"]["unit_code"] for row in validation])
    unit_predictions = np.array([unit_by_topic[code] for code in top[:, 0]])

    def metrics(mask):
        count = int(mask.sum())
        if not count:
            return {"n": 0}
        truth, predicted = y_validation[mask], top[mask]
        return {
            "n": count,
            "top1_accuracy": float(np.mean(predicted[:, 0] == truth)),
            "top3_accuracy": float(np.mean(np.any(predicted[:, :3] == truth[:, None], axis=1))),
            "top5_accuracy": float(np.mean(np.any(predicted == truth[:, None], axis=1))),
            "macro_f1": float(f1_score(truth, predicted[:, 0], labels=np.unique(truth), average="macro", zero_division=0)),
            "unit_accuracy": float(np.mean(unit_predictions[mask] == unit_truth[mask])),
        }

    report = {
        "mode": mode, "training_n": len(train), "training_topics": len(model.classes_),
        "validation_n": len(validation), "validation_topics": len(set(y_validation)),
        "unseen_validation_topic_codes": sorted(set(y_validation) - set(y_train)),
        "exact_input_overlap": int(exact_overlap.sum()), "near_template_overlap": int(near_overlap.sum()),
        "official_validation_all": metrics(np.ones(len(validation), dtype=bool)),
        "without_exact_input_overlap": metrics(~exact_overlap),
        "without_near_template_overlap": metrics(~near_overlap & ~exact_overlap),
        "seconds": round(time.perf_counter() - started, 2),
        "ocr_executed": False,
    }
    with (output / f"{mode}_predictions.jsonl").open("x", encoding="utf-8", newline="\n") as stream:
        for i, row in enumerate(validation):
            stream.write(json.dumps({
                "id": row["id"], "actual_topic": row["label"]["source_topic_code"],
                "predicted_topics": top[i].tolist(), "correct_top1": bool(top[i, 0] == y_validation[i]),
                "exact_input_overlap": bool(exact_overlap[i]),
                "nearest_numeric_template_similarity": round(float(nearest_template[i]), 5),
                "provided_figure_text": bool(row["input"]["figure_text"]),
            }, ensure_ascii=False) + "\n")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train", type=Path, default=RAW_DIR / "train.jsonl")
    parser.add_argument("--validation", type=Path, default=RAW_DIR / "validation.jsonl")
    parser.add_argument("--output-dir", type=Path, default=RAW_DIR / "evaluation_v1")
    parser.add_argument("--modes", nargs="+", choices=("body", "body_figure"), default=["body", "body_figure"])
    args = parser.parse_args()
    try:
        modes = list(dict.fromkeys(args.modes))
        outputs = [args.output_dir / "evaluation.json"] + [args.output_dir / f"{mode}_predictions.jsonl" for mode in modes]
        if any(path.exists() for path in outputs):
            raise ValueError("기존 평가 결과를 덮어쓰지 않습니다. 다른 --output-dir를 사용하세요.")
        train = load_split(args.train, "Training")
        validation = load_split(args.validation, "Validation")
        args.output_dir.mkdir(parents=True, exist_ok=True)
        results = []
        for mode in modes:
            print(f"평가 시작: {mode} / 학습 {len(train):,}, 공식 Validation {len(validation):,}", flush=True)
            report = evaluate(train, validation, mode, args.output_dir)
            results.append(report)
            print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        with (args.output_dir / "evaluation.json").open("x", encoding="utf-8") as stream:
            json.dump({"model": "TF-IDF character 2-5gram + LinearSVC C=10 balanced", "results": results}, stream, ensure_ascii=False, indent=2)
    except (OSError, ValueError, KeyError, ImportError) as error:
        print(f"평가 실패: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
