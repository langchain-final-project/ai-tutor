"""합성 ZIP/DB로 원천 연결, 평가 분리, 훼손 감지와 영역 자르기를 검증한다."""

from contextlib import closing
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import struct
import sys
import tempfile
import unittest
import zipfile
import zlib

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
from app.db.raw_problem_data import RawProblemStore, archive_path  # noqa: E402

spec = importlib.util.spec_from_file_location("prepare_raw_problem_data", BACKEND / "scripts/prepare_raw_problem_data.py")
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


def small_png() -> bytes:
    def chunk(kind, content):
        return struct.pack(">I", len(content)) + kind + content + struct.pack(">I", zlib.crc32(kind + content) & 0xffffffff)
    scanlines = (b"\0" + b"\xff\0\0" * 16) * 10
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 16, 10, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(scanlines)) + chunk(b"IEND", b""))


def label(question_id, text, code):
    info = {"question_grade": "M1", "question_term": 1, "question_unit": "01",
            "question_topic": code, "question_topic_name": "test-" + code, "question_type1": "단답형",
            "question_difficulty": 2, "question_sector1": "이해", "question_sector2": "수와 연산", "question_step": "기본"}
    boxes = [{"type": "line", "bbox": [0, 0, 16, 10], "text": None},
             {"type": "paragraph", "bbox": [2, 2, 14, 8], "text": text}]
    question = {"question_filename": question_id, "question_info": [info],
                "OCR_info": [{"question_text": text, "figure_text": None, "question_bbox": boxes}]}
    answer = {"question_filename": question_id, "question_info": [info], "answer_info": [{
        "answer_filename": Path(question_id).stem + "_A.png", "answer_text": "test answer",
        "answer_bbox": [{"type": "answer", "bbox": [1, 1, 8, 8], "text": "1"}],
    }]}
    return question, answer


class RawProblemDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.source = self.folder / "source"
        self.source.mkdir()
        self.bank = self.folder / "bank.sqlite3"
        self.database = self.folder / "derived" / "sources.sqlite3"
        self.q1 = "M1_1_01_fixture01.png"
        self.q2 = "M1_1_01_fixture02.png"
        self.v1 = "M1_1_01_fixture03.png"
        self.training = [label(self.q1, "방정식 2x+1=3을 풀어라.", "T1"),
                         label(self.q2, "수직선에서 두 점 사이의 거리를 구하여라.", "T2")]
        self.validation = [label(self.v1, self.training[1][0]["OCR_info"][0]["question_text"], "T3")]
        with closing(sqlite3.connect(self.bank)) as connection:
            connection.executescript("""CREATE TABLE questions (
                id TEXT PRIMARY KEY,grade INTEGER,question_text TEXT,figure_text TEXT,
                source_topic_code TEXT,answer_filename TEXT);
                CREATE TABLE benchmark_items (question_id TEXT);""")
            for q, a in self.training:
                connection.execute("INSERT INTO questions VALUES (?,1,?,NULL,?,?)", (
                    q["question_filename"], q["OCR_info"][0]["question_text"],
                    q["question_info"][0]["question_topic"], a["answer_info"][0]["answer_filename"],
                ))
            connection.execute("INSERT INTO benchmark_items VALUES (?)", (self.q1,))
            connection.commit()
        self.write_archives()

    def tearDown(self):
        self.temp.cleanup()

    def write_archives(self):
        for split, prefix, items in (("Training", "T", self.training), ("Validation", "V", self.validation)):
            folder = self.source / split
            folder.mkdir(exist_ok=True)
            for role, letter in (("question_image", "S_1"), ("answer_image", "S_2"), ("question_label", "L_1"), ("answer_label", "L_2")):
                path = folder / f"{prefix}{letter}.문제_중학교_1학년.zip"
                with zipfile.ZipFile(path, "w") as archive:
                    for q, a in items:
                        qid = q["question_filename"]
                        if role == "question_image":
                            archive.writestr("/" + qid, small_png())
                        elif role == "answer_image":
                            archive.writestr("/" + a["answer_info"][0]["answer_filename"], small_png())
                        elif role == "question_label":
                            archive.writestr("/" + Path(qid).stem + ".json", json.dumps(q, ensure_ascii=False))
                        else:
                            archive.writestr("/" + Path(qid).stem + "_A.json", json.dumps(a, ensure_ascii=False))

    def build(self):
        return prepare.build(self.database, self.source, self.bank, [1])

    def test_links_all_images_without_changing_bank(self):
        before = hashlib.sha256(self.bank.read_bytes()).hexdigest()
        report = self.build()
        self.assertEqual(report["questions"], {"Training": 2, "Validation": 1})
        self.assertEqual(report["images"], 6)
        self.assertEqual(report["bank_links"], 2)
        self.assertEqual(hashlib.sha256(self.bank.read_bytes()).hexdigest(), before)
        store = RawProblemStore(self.database, self.source)
        self.assertEqual(store.read_image(self.q1), small_png())
        self.assertEqual(store.read_image(self.v1, "answer", "Validation"), small_png())
        self.assertIsNone(store.get_problem(self.v1, "Validation")["bank_question_id"])

    def test_reserves_benchmark_and_keeps_validation_separate(self):
        report = self.build()
        self.assertEqual(report["reserved_ocr50"], 1)
        self.assertEqual(report["classifier_training_pool"], 1)
        self.assertEqual(report["validation_unseen_topic_codes"], ["T3"])
        self.assertEqual(report["validation_same_input_as_training"], 1)
        prepare.export_inputs(self.database, self.database.parent)
        train = [json.loads(line) for line in (self.database.parent / "train.jsonl").read_text(encoding="utf-8").splitlines()]
        validation = [json.loads(line) for line in (self.database.parent / "validation.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([r["id"] for r in train], [self.q2])
        self.assertEqual([r["id"] for r in validation], [self.v1])
        self.assertTrue(validation[0]["same_input_in_training_pool"])
        self.assertEqual(set(train[0]["input"]), {"question_text", "figure_text"})
        self.assertNotIn("answer_text", train[0])
        self.assertEqual(train[0]["question_image"]["kind"], "question")

    def test_bbox_text_and_metadata_are_preserved(self):
        self.build()
        store = RawProblemStore(self.database, self.source)
        regions = store.get_regions(self.q1)
        self.assertIsNone(regions[0]["text"])
        self.assertEqual(regions[1]["bbox"], [2, 2, 14, 8])
        self.assertEqual(store.get_problem(self.q1)["sector2"], "수와 연산")

    def test_existing_database_is_never_overwritten(self):
        self.build()
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        with self.assertRaises(FileExistsError):
            self.build()
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).hexdigest(), before)

    def test_changed_archive_is_detected(self):
        self.build()
        archive = next(self.source.rglob("TS_1.*.zip"))
        with zipfile.ZipFile(archive, "a") as z:
            z.writestr("changed.txt", "changed")
        with self.assertRaisesRegex(ValueError, "ZIP이 변경"):
            prepare.verify(self.database, self.source)

    def test_wrong_bank_topic_rolls_back_new_database(self):
        with closing(sqlite3.connect(self.bank)) as connection:
            connection.execute("UPDATE questions SET source_topic_code='WRONG' WHERE id=?", (self.q1,))
            connection.commit()
        with self.assertRaisesRegex(ValueError, "문제은행 불일치"):
            self.build()
        self.assertFalse(self.database.exists())

    def test_read_image_detects_wrong_hash(self):
        self.build()
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("UPDATE source_assets SET sha256=? WHERE question_id=?", ("0"*64, self.q1))
            connection.commit()
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            RawProblemStore(self.database, self.source).read_image(self.q1)

    def test_archive_reference_cannot_escape_source_root(self):
        with self.assertRaisesRegex(ValueError, "폴더 밖"):
            archive_path(self.source, "../outside.zip")

    def test_duplicate_member_filenames_are_rejected(self):
        path = self.folder / "duplicates.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("a/example.png", small_png())
            archive.writestr("b/example.png", small_png())
        with zipfile.ZipFile(path) as archive:
            with self.assertRaisesRegex(ValueError, "파일명 중복"):
                prepare.member_map(archive, ".png")

    def test_crop_region(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow가 없는 환경: 영역 조회/연결 테스트는 별도로 실행")
        self.build()
        store = RawProblemStore(self.database, self.source)
        output = self.folder / "crop.png"
        report = store.crop_region(self.q1, output, region_index=1)
        self.assertFalse(report["clamped"])
        with Image.open(output) as image:
            self.assertEqual(image.size, (12, 6))
        before = output.read_bytes()
        with self.assertRaises(FileExistsError):
            store.crop_region(self.q1, output, region_index=1)
        self.assertEqual(output.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
