"""모델 저장·재로딩의 동등성, 학습 누출 방지, 입력 계약과의 연결을 검사한다."""

import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.svm import LinearSVC

from app.tutor.classification_contract import ClassificationRequest
from app.tutor.type_classifier import (
    MiddleSchoolTypeClassifier, TypeClassifier, VECTORIZER_OPTIONS,
    encode_request, model_directory, normalize_input,
)
from app.tutor.type_classifier_training import load_training_corpus, train_classifier, train_school_classifiers


def confirmed(**changes):
    return {"question_text": "일차방정식 2x+1=5를 풀어라.", "grade": 1,
            "user_confirmed": True, "has_figure": False, "has_table": False, **changes}


def create_bank(path):
    with closing(sqlite3.connect(path)) as db:
        db.executescript("""
            CREATE TABLE topics(source_topic_code TEXT PRIMARY KEY, source_topic_name TEXT);
            CREATE TABLE questions(id TEXT PRIMARY KEY, grade INTEGER, question_text TEXT,
                figure_text TEXT, source_topic_code TEXT, answer TEXT);
            CREATE TABLE benchmark_items(question_id TEXT);
        """)
        db.executemany("INSERT INTO topics VALUES (?,?)", [
            ("7103047", "EQUATION_LABEL_ONLY"), ("7101001", "PRIME_LABEL_ONLY"),
            ("7206012", "GEOMETRY_LABEL_ONLY"),
        ])
        for i in range(4):
            for prefix, code, text, figure in [
                ("eq", "7103047", f"일차방정식 {i+2}x+1={i+5}를 풀어라.", None),
                ("prime", "7101001", f"자연수 {i+20}을 소인수분해하여라.", None),
                ("figure", "7206012", "삼각형의 각 x의 크기를 구하여라.", f"삼각형 ABC에서 각 A={i+30}도, 각 B=60도"),
            ]:
                db.execute("INSERT INTO questions VALUES (?,?,?,?,?,?)",
                           (f"{prefix}{i}", 1, text, figure, code, "SECRET_ANSWER"))
        for qid, grade, text in [
            ("held", 1, "수 17의 소수 여부를 판별하시오."),
            ("held_template", 1, "수 19의 소수 여부를 판별하시오."),
            ("middle2", 2, "연립방정식을 풀어라."),
        ]:
            db.execute("INSERT INTO questions VALUES (?,?,?,?,?,?)", (qid, grade, text, None, "7101001", "SECRET_ANSWER"))
        db.execute("INSERT INTO benchmark_items VALUES ('held')")
        db.commit()


def create_school_bank(path):
    create_bank(path)
    with closing(sqlite3.connect(path)) as db:
        db.execute("DELETE FROM questions WHERE id='middle2'")
        for grade, examples in [
            (2, [("8101001", "유리수의 순환소수 표현", "유리수와 순환소수의 관계를 구하여라."),
                 ("8102001", "연립방정식 풀이", "연립방정식 x+y=3, x-y=1을 풀어라.")]),
            (3, [("9101001", "제곱근 계산", "제곱근과 실수의 값을 계산하여라."),
                 ("9102001", "이차방정식 풀이", "이차방정식 x^2-3x+2=0을 풀어라.")]),
        ]:
            for code, name, text in examples:
                db.execute("INSERT INTO topics VALUES (?,?)", (code, name))
                for i in range(3):
                    db.execute("INSERT INTO questions VALUES (?,?,?,?,?,?)",
                               (f"grade{grade}-{code}-{i}", grade, text, None, code, "SECRET_ANSWER"))
        db.commit()


class TypeClassifierTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="didgo-type-model-test-")
        cls.root = Path(cls.temporary.name).resolve()
        cls.database = cls.root / "bank.sqlite3"
        cls.directory = cls.root / "model"
        create_bank(cls.database)
        cls.summary = train_classifier(cls.database, cls.directory)
        cls.model = TypeClassifier.load(cls.directory)

    @classmethod
    def tearDownClass(cls):
        assert cls.root.is_relative_to(Path(tempfile.gettempdir()).resolve())
        cls.temporary.cleanup()

    def test_reserved_numeric_templates_and_other_grades_are_excluded(self):
        rows, summary = load_training_corpus(self.database)
        self.assertEqual(len(rows), 12)
        self.assertTrue({"held", "held_template", "middle2"}.isdisjoint({r["id"] for r in rows}))
        self.assertEqual(summary["reserved_questions"], 1)

    def test_saved_heads_match_original_sklearn_decision_values(self):
        rows, _ = load_training_corpus(self.database)
        for mode in ("body", "body_figure"):
            texts = [normalize_input(r["question_text"] + (
                " " + (r["figure_text"] or "") if mode == "body_figure" else "")) for r in rows]
            vectorizer = TfidfVectorizer(**VECTORIZER_OPTIONS, dtype=np.float32)
            features = vectorizer.fit_transform(texts)
            reference = LinearSVC(C=10, class_weight="balanced", dual="auto", max_iter=4000, random_state=42)
            reference.fit(features, [r["source_topic_code"] for r in rows])
            scores, _ = self.model.heads[mode].score_texts(texts)
            np.testing.assert_allclose(scores, reference.decision_function(features), atol=1e-10)
            # 유형 이름과 정답 필드는 입력 특징에 넣지 않는다.
            words = self.model.heads[mode].vectorizer.vocabulary_
            self.assertNotIn("LABEL", words)
            self.assertNotIn("SECRE", words)

    def test_prediction_matches_contract_and_never_confirms_a_type(self):
        result = self.model.predict(confirmed(), top_k=2)
        self.assertEqual(result.status, "candidates")
        self.assertEqual(len(result.candidates), 2)
        self.assertEqual(result.candidates[0].score_name, "linear_svc_margin")
        self.assertNotIn("source_topic_code", result.model_dump())
        self.assertEqual(result.candidates[0].reference_question_ids, [])

    def test_grade_and_missing_information_are_checked_before_scoring(self):
        with patch.object(self.model.heads["body"], "score_texts", side_effect=AssertionError("must not score")):
            self.assertEqual(self.model.predict(confirmed(grade=2)).status, "unsupported")
            result = self.model.predict(confirmed(user_confirmed=False))
            self.assertEqual(result.status, "needs_clarification")
            self.assertEqual(result.questions[0].field, "confirmation")

    def test_structured_input_is_preserved_and_figure_head_is_used(self):
        request = ClassificationRequest.model_validate(confirmed(
            equations=["x=1", "x=10"], conditions=["BC=6cm", "DE=6cm"], targets=["각 x"],
            has_figure=True, figures=[{"id": "triangle", "conditions": ["A(-5,2)", "B(4,-2)"]}],
        ))
        main, figure = encode_request(request)
        for value in ["x=1", "x=10", "BC=6cm", "DE=6cm"]:
            self.assertIn(value, main)
        self.assertIn("A(-5,2)", figure)
        with patch.object(self.model.heads["body"], "score_texts", side_effect=AssertionError("wrong head")):
            self.assertEqual(self.model.predict(request).status, "candidates")

    def test_no_known_features_does_not_produce_arbitrary_type(self):
        self.assertEqual(self.model.predict(confirmed(question_text="🦄🦄🦄")).status, "needs_clarification")

    def test_corrupted_weights_and_wrong_preprocessing_version_are_rejected(self):
        for case in ["weights", "version"]:
            directory = self.root / case
            directory.mkdir()
            for name in ["manifest.json", "body.npz", "body_figure.npz"]:
                shutil.copyfile(self.directory / name, directory / name)
            if case == "weights":
                with (directory / "body.npz").open("ab") as stream:
                    stream.write(b"corrupt")
            else:
                path = directory / "manifest.json"
                metadata = json.loads(path.read_text(encoding="utf-8"))
                metadata["preprocessor_version"] = "different"
                path.write_text(json.dumps(metadata), encoding="utf-8")
            with self.subTest(case=case), self.assertRaises(ValueError):
                TypeClassifier.load(directory)

    def test_training_does_not_modify_db_or_overwrite_model(self):
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        with self.assertRaises(ValueError):
            train_classifier(self.database, self.directory)
        self.assertEqual(before, hashlib.sha256(self.database.read_bytes()).hexdigest())

    def test_binary_classifier_export_has_one_margin_per_type(self):
        database = self.root / "binary.sqlite3"
        create_bank(database)
        with closing(sqlite3.connect(database)) as db:
            db.execute("UPDATE questions SET grade=2 WHERE source_topic_code='7206012'")
            db.commit()
        directory = self.root / "binary-model"
        train_classifier(database, directory)
        model = TypeClassifier.load(directory)
        scores, _ = model.heads["body"].score_texts([normalize_input(confirmed()["question_text"])])
        self.assertEqual(scores.shape, (1, 2))
        self.assertAlmostEqual(float(scores[0, 0]), -float(scores[0, 1]))


class MiddleSchoolRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="didgo-school-routing-test-")
        cls.root = Path(cls.temporary.name).resolve()
        cls.database = cls.root / "bank.sqlite3"
        cls.models = cls.root / "models"
        create_school_bank(cls.database)
        cls.report = train_school_classifiers(cls.database, cls.models)

    @classmethod
    def tearDownClass(cls):
        assert cls.root.is_relative_to(Path(tempfile.gettempdir()).resolve())
        cls.temporary.cleanup()

    def test_all_grades_train_without_requiring_middle1_benchmark_in_other_grades(self):
        self.assertEqual(self.report["total_types"], 7)
        self.assertEqual([r["grade"] for r in self.report["grades"]], [1, 2, 3])
        for grade in [2, 3]:
            rows, summary = load_training_corpus(self.database, grade=grade)
            self.assertEqual(summary["reserved_questions"], 0)
            self.assertEqual(len(rows), 6)
            self.assertTrue(all(row["grade"] == grade for row in rows))

    def test_routing_returns_only_types_from_requested_grade(self):
        router = MiddleSchoolTypeClassifier(self.models)
        for grade in [1, 2, 3]:
            with self.subTest(grade=grade):
                result = router.predict(confirmed(grade=grade))
                selected = router._models[grade]
                self.assertEqual(selected.grade, grade)
                self.assertEqual(result.status, "candidates")
                self.assertTrue({c.source_topic_code for c in result.candidates}.issubset(
                    {c["code"] for c in selected.classes}))
        self.assertEqual(set(router._models), {1, 2, 3})

    def test_missing_grade_does_not_load_or_guess_a_model(self):
        payload = confirmed()
        payload.pop("grade")
        with patch("app.tutor.type_classifier.TypeClassifier.load", side_effect=AssertionError("must not load")):
            result = MiddleSchoolTypeClassifier(self.root / "no-models").predict(payload)
        self.assertEqual(result.status, "needs_clarification")
        self.assertIn("grade", [q.field for q in result.questions])

    def test_single_grade_model_rejects_another_grade(self):
        for grade in [1, 2, 3]:
            model = TypeClassifier.load(model_directory(grade, self.models))
            other = 1 if grade != 1 else 2
            self.assertEqual(model.predict(confirmed(grade=other)).status, "unsupported")

    def test_existing_models_are_verified_and_reused_without_rewriting(self):
        paths = list(self.models.glob("*/*"))
        before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
        report = train_school_classifiers(self.database, self.models)
        self.assertTrue(all(row["status"] == "reused" for row in report["grades"]))
        self.assertEqual(before, {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths})

    def test_changed_training_source_is_not_silently_reused(self):
        copied = self.root / "changed.sqlite3"
        shutil.copyfile(self.database, copied)
        with closing(sqlite3.connect(copied)) as db:
            db.execute("UPDATE questions SET question_text=question_text || '추가 조건' WHERE grade=3")
            db.commit()
        with self.assertRaises(ValueError):
            train_school_classifiers(copied, self.models)

    def test_cli_grade_selection_and_missing_grade(self):
        script = Path(__file__).resolve().parents[1] / "scripts/predict_problem_type.py"
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        common = [sys.executable, "-B", str(script), "--text", "이차방정식을 풀어라.", "--confirmed"]
        selected = subprocess.run(common + ["--model-root", str(self.models), "--grade", "3"],
                                  env=env, capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(selected.returncode, 0, selected.stderr)
        output = json.loads(selected.stdout)
        self.assertEqual(output["status"], "candidates")
        self.assertTrue(all(c["source_topic_code"].startswith("9") for c in output["candidates"]))
        missing = subprocess.run(common + ["--model-root", str(self.root / "missing-models")],
                                 env=env, capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(missing.returncode, 0, missing.stderr)
        self.assertEqual(json.loads(missing.stdout)["status"], "needs_clarification")


if __name__ == "__main__":
    unittest.main()
