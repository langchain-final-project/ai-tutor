"""문제 사진 → OCR → 수학 유형 연결과 잘못된 결과 저장 방지 검증."""

from contextlib import closing
import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "setup_problem_classification.py"
spec = importlib.util.spec_from_file_location("setup_problem_classification", SCRIPT)
classification = importlib.util.module_from_spec(spec)
spec.loader.exec_module(classification)


class ProblemClassificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.database = Path(cls.temp.name) / "classification.sqlite3"
        cls.data = classification.base.load_sources(classification.base.SOURCE_DIR)
        classification.base.build(cls.database, cls.data)
        with closing(sqlite3.connect(cls.database)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            classification.apply_extension(connection)
        cls.question = cls.data["questions"][cls.data["items"][0]["question_id"]]
        cls.other = next(q for q in cls.data["questions"].values()
                         if q["source_topic_code"] != cls.question["source_topic_code"])

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.connection = sqlite3.connect(self.database)
        self.connection.execute("PRAGMA foreign_keys = ON")

    def tearDown(self):
        self.connection.rollback()
        self.connection.close()

    def photo_and_ocr(self, status="completed"):
        # 실제 모델을 호출하지 않고 원본 비교용 이미지를 테스트 사진으로 사용한다.
        asset = self.data["assets"][self.data["items"][0]["question_asset"]]
        upload_id = self.connection.execute(
            "INSERT INTO uploaded_problems (original_filename, mime_type, image_sha256, image_content) VALUES (?, ?, ?, ?)",
            ("test.png", "image/png", asset["sha256"], asset["content"]),
        ).lastrowid
        result_id = self.connection.execute(
            """INSERT INTO problem_ocr_results
               (uploaded_problem_id, model_name, pipeline_version, status, ocr_text, error_message)
               VALUES (?, 'test-fixture', 'test-v1', ?, ?, ?)""",
            (upload_id, status, self.question["question_text"] if status == "completed" else None,
             "test error" if status == "failed" else None),
        ).lastrowid
        return upload_id, result_id

    def classify(self, ocr_id, question_id=None, topic_code=None, status="classified", confidence=0.9):
        return self.connection.execute(
            """INSERT INTO problem_classifications
               (ocr_result_id, method, pipeline_version, status, source_topic_code, matched_question_id, confidence)
               VALUES (?, 'retrieval', 'test-v1', ?, ?, ?, ?)""",
            (ocr_id, status, topic_code, question_id, confidence),
        ).lastrowid

    def test_base_bank_and_benchmark_remain_unchanged(self):
        report = classification.base.verify(self.connection, self.data)
        self.assertEqual(report["questions"], 30339)
        self.assertEqual(report["benchmark_items"], 50)
        self.assertEqual(report["benchmark_sha256"], "393e7c7c905074979c43012cb83e779fe2265cc12355bd950765c386cf027207")

    def test_topic_catalog_and_corpus(self):
        report = classification.verify_extension(self.connection)
        self.assertEqual(report["classification_topics"], 1165)
        self.assertEqual(report["classification_corpus"], 30339)
        self.assertEqual(report["uploaded_problems"], 0)
        self.assertEqual(self.connection.execute(
            "SELECT SUM(question_count) FROM problem_topic_catalog").fetchone()[0], 30339)
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(question_classification_corpus)")}
        self.assertFalse({"answer", "answer_text", "answer_box_texts"} & columns)

    def test_photo_ocr_classification_and_candidate_round_trip(self):
        upload_id, ocr_id = self.photo_and_ocr()
        result_id = self.classify(ocr_id, self.question["id"], self.question["source_topic_code"])
        self.connection.execute(
            """INSERT INTO problem_classification_candidates
               (classification_id, rank, source_topic_code, matched_question_id, score, score_name)
               VALUES (?, 1, ?, ?, 0.9, 'test_similarity')""",
            (result_id, self.question["source_topic_code"], self.question["id"]),
        )
        row = self.connection.execute(
            "SELECT uploaded_problem_id, source_topic_code, source_topic_name, matched_question_id FROM classified_problems WHERE classification_id = ?",
            (result_id,),
        ).fetchone()
        self.assertEqual(row, (upload_id, self.question["source_topic_code"],
                               self.question["source_topic_name"], self.question["id"]))
        self.assertEqual(classification.verify_extension(self.connection)["problem_classifications"], 1)

    def test_new_question_can_have_topic_without_bank_match(self):
        _, ocr_id = self.photo_and_ocr()
        result_id = self.classify(ocr_id, topic_code=self.question["source_topic_code"])
        row = self.connection.execute(
            "SELECT source_topic_name, matched_question_id FROM classified_problems WHERE classification_id = ?",
            (result_id,),
        ).fetchone()
        self.assertEqual(row, (self.question["source_topic_name"], None))

    def test_unknown_and_failed_results_are_not_forced_into_topic(self):
        _, ocr_id = self.photo_and_ocr()
        result_id = self.classify(ocr_id, status="unclassified", confidence=None)
        self.assertEqual(self.connection.execute(
            "SELECT status, source_topic_code FROM classified_problems WHERE classification_id = ?",
            (result_id,),
        ).fetchone(), ("unclassified", None))
        with self.assertRaises(sqlite3.IntegrityError):
            self.classify(ocr_id, topic_code="unknown-topic")
        with self.assertRaises(sqlite3.IntegrityError):
            self.classify(ocr_id, topic_code=self.question["source_topic_code"], confidence=1.1)

    def test_mismatching_question_and_topic_rejected(self):
        _, ocr_id = self.photo_and_ocr()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "topic mismatch"):
            self.classify(ocr_id, self.question["id"], self.other["source_topic_code"])
        result_id = self.classify(ocr_id, self.question["id"], self.question["source_topic_code"])
        with self.assertRaisesRegex(sqlite3.IntegrityError, "topic mismatch"):
            self.connection.execute("UPDATE problem_classifications SET source_topic_code = ? WHERE id = ?",
                                    (self.other["source_topic_code"], result_id))
        with self.assertRaisesRegex(sqlite3.IntegrityError, "topic mismatch"):
            self.connection.execute(
                "INSERT INTO problem_classification_candidates (classification_id, rank, source_topic_code, matched_question_id) VALUES (?, 1, ?, ?)",
                (result_id, self.other["source_topic_code"], self.question["id"]),
            )

    def test_failed_ocr_cannot_be_classified_or_replace_classified_ocr(self):
        _, failed_ocr = self.photo_and_ocr(status="failed")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "completed OCR"):
            self.classify(failed_ocr, topic_code=self.question["source_topic_code"])
        _, completed_ocr = self.photo_and_ocr()
        result_id = self.classify(completed_ocr, topic_code=self.question["source_topic_code"])
        with self.assertRaisesRegex(sqlite3.IntegrityError, "completed OCR"):
            self.connection.execute("UPDATE problem_classifications SET ocr_result_id = ? WHERE id = ?", (failed_ocr, result_id))
        with self.assertRaisesRegex(sqlite3.IntegrityError, "must remain completed"):
            self.connection.execute("UPDATE problem_ocr_results SET status = 'failed', error_message = 'test' WHERE id = ?", (completed_ocr,))

    def test_reapplying_extension_preserves_uploads(self):
        self.photo_and_ocr()
        # 커밋 없이도 재적용 경로가 기존 데이터를 건드리지 않는지 검사한다.
        report = classification.apply_extension(self.connection)
        self.assertEqual(report["uploaded_problems"], 1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM problem_schema_migrations").fetchone()[0], 1)

    def test_migration_failure_rolls_back_schema(self):
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.executescript(classification.base.SCHEMA.read_text(encoding="utf-8"))
            with patch.object(classification, "verify_extension", side_effect=ValueError("test failure")):
                with self.assertRaisesRegex(ValueError, "test failure"):
                    classification.apply_extension(connection)
            names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
            self.assertNotIn("uploaded_problems", names)
            self.assertIn("questions", names)


if __name__ == "__main__":
    unittest.main()
