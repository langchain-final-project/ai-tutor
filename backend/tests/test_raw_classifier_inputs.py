"""학습 split과 모델 입력으로 정답 메타데이터가 섞이는 실수를 방지한다."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "experiments/evaluate_raw_classifier.py"
spec = importlib.util.spec_from_file_location("evaluate_raw_classifier", SCRIPT)
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)


class RawClassifierInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "input.jsonl"
        self.row = {"id": "example.png", "split": "Training",
                    "input": {"question_text": "문제 본문", "figure_text": "그림 조건"},
                    "label": {"source_topic_name": "분류 정답 유형"}}

    def tearDown(self):
        self.temp.cleanup()

    def write(self, rows):
        self.path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")

    def test_validation_cannot_be_loaded_as_training(self):
        self.row["split"] = "Validation"
        self.write([self.row])
        with self.assertRaisesRegex(ValueError, "split이 섞였"):
            evaluation.load_split(self.path, "Training")

    def test_duplicate_ids_are_rejected(self):
        self.write([self.row, self.row])
        with self.assertRaisesRegex(ValueError, "ID가 중복"):
            evaluation.load_split(self.path, "Training")

    def test_input_cannot_contain_topic_label(self):
        self.row["input"]["source_topic_name"] = "정답 유형"
        self.write([self.row])
        with self.assertRaisesRegex(ValueError, "본문과 제공된 그림 설명만"):
            evaluation.load_split(self.path, "Training")

    def test_only_available_input_fields_are_used(self):
        self.write([self.row])
        rows = evaluation.load_split(self.path, "Training")
        self.assertEqual(evaluation.input_text(rows[0], "body"), "문제본문")
        self.assertEqual(evaluation.input_text(rows[0], "body_figure"), "문제본문그림조건")
        self.assertNotIn("분류정답유형", evaluation.input_text(rows[0], "body_figure"))


if __name__ == "__main__":
    unittest.main()
