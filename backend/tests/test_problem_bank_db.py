"""실제 원본 전체와의 대조 및 DB 생성 실패 시 데이터 보존 검사."""

import hashlib
from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_problem_bank.py"
spec = importlib.util.spec_from_file_location("build_problem_bank", SCRIPT)
db = importlib.util.module_from_spec(spec)
spec.loader.exec_module(db)


class ProblemBankTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.database = Path(cls.temp.name) / "test.sqlite3"
        cls.data = db.load_sources(db.SOURCE_DIR)
        cls.report = db.build(cls.database, cls.data)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_full_round_trip_and_integrity(self):
        with closing(sqlite3.connect(self.database)) as connection:
            report = db.verify(connection, self.data)
        self.assertEqual(report["questions"], 30339)
        self.assertEqual(report["by_grade"], {1: 10674, 2: 10236, 3: 9429})
        self.assertEqual(report["benchmark_items"], 50)
        self.assertEqual(report["benchmark_images"], 100)

    def test_benchmark_order_and_training_exclusion(self):
        with closing(sqlite3.connect(self.database)) as connection:
            selected = connection.execute("SELECT position, id FROM ocr50_questions ORDER BY position").fetchall()
            self.assertEqual(selected, [(item["position"], item["question_id"]) for item in self.data["items"]])
            overlap = connection.execute(
                "SELECT COUNT(*) FROM training_questions t JOIN benchmark_items b ON t.id = b.question_id"
            ).fetchone()[0]
            self.assertEqual(overlap, 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM training_questions").fetchone()[0], 30289)

    def test_missing_answer_and_leading_zero_codes_preserved(self):
        with closing(sqlite3.connect(self.database)) as connection:
            missing = connection.execute("SELECT id FROM questions WHERE answer IS NULL").fetchall()
            self.assertEqual(missing, [("M1_2_07_00041_86343.png",)])
            unit_code, source_id, tags = connection.execute(
                "SELECT unit_code, source_id, skill_tags FROM questions WHERE id = ?",
                ("M1_1_01_00041_42357.png",),
            ).fetchone()
            self.assertEqual(unit_code, "01")
            self.assertEqual(source_id, "00041_42357")
            self.assertIsInstance(json.loads(tags), list)

    def test_existing_database_cannot_be_overwritten(self):
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        with self.assertRaises(FileExistsError):
            db.build(self.database, self.data)
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).hexdigest(), before)

    def test_failure_removes_only_new_database(self):
        failed_path = Path(self.temp.name) / "failed.sqlite3"
        with patch.object(db, "insert_data", side_effect=ValueError("simulated import failure")):
            with self.assertRaisesRegex(ValueError, "simulated import failure"):
                db.build(failed_path, self.data)
        self.assertFalse(failed_path.exists())
        self.assertTrue(self.database.exists())

    def test_foreign_keys_reject_unknown_question(self):
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE benchmark_items SET question_id = 'missing' WHERE position = 1"
                )
            connection.rollback()

    def test_verify_detects_modified_question_and_image(self):
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("UPDATE questions SET answer = 'modified' WHERE id = ?",
                               (self.data["items"][0]["question_id"],))
            with self.assertRaisesRegex(ValueError, "원본 불일치"):
                db.verify(connection, self.data)
            connection.rollback()
            connection.execute("UPDATE benchmark_assets SET content = X'00' WHERE source_member = ?",
                               (self.data["items"][0]["question_asset"],))
            with self.assertRaisesRegex(ValueError, "이미지 바이트/해시 불일치"):
                db.verify(connection, self.data)
            connection.rollback()


if __name__ == "__main__":
    unittest.main()
