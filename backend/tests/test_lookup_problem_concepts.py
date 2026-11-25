"""개념 카드 전체 범위와 문제별 예외 연결을 검증한다."""

import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "lookup_problem_concepts.py"
spec = importlib.util.spec_from_file_location("lookup_problem_concepts", SCRIPT)
lookup_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lookup_module)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def concept_document(grade, semester, unit_code, concept_ids):
    return {
        "grade": grade,
        "semester": semester,
        "unit_code": unit_code,
        "unit_name": f"단원 {unit_code}",
        "sections": [
            {
                "title": "대단원",
                "subsections": [
                    {
                        "title": "소단원",
                        "concepts": [
                            {
                                "concept_id": concept_id,
                                "title": concept_id,
                                "definition": "테스트 개념",
                                "rules": [],
                            }
                            for concept_id in concept_ids
                        ],
                    }
                ],
            }
        ],
    }


class LookupProblemConceptsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "concept_bank"
        self.database = Path(self.temp.name) / "questions.sqlite3"

        write_json(
            self.root / "m1" / "m1_s1_u01_concepts.json",
            concept_document(1, 1, "01", ["m1.s1.u01.c001", "m1.s1.u01.c002"]),
        )
        write_json(
            self.root / "m2" / "m2_s1_u01_concepts.json",
            concept_document(2, 1, "01", ["m2.s1.u01.c001"]),
        )
        write_json(
            self.root / "m3" / "m3_s2_u01_concepts.json",
            concept_document(3, 2, "01", ["m3.s2.u01.c001"]),
        )
        write_json(
            self.root / "topic_concept_map.json",
            [
                {
                    "grade": 1,
                    "semester": 1,
                    "unit_code": "01",
                    "source_topic_code": "7101001",
                    "source_topic_name": "중1 토픽",
                    "match_kind": "topic",
                    "concept_ids": ["m1.s1.u01.c001"],
                },
                {
                    "grade": 2,
                    "semester": 1,
                    "unit_code": "01",
                    "source_topic_code": "8101001",
                    "source_topic_name": "중2 토픽",
                    "match_kind": "unit_fallback",
                    "concept_ids": [],
                },
                {
                    "grade": 3,
                    "semester": 2,
                    "unit_code": "01",
                    "source_topic_code": "9301001",
                    "source_topic_name": "중3 토픽",
                    "match_kind": "related",
                    "concept_ids": ["m3.s2.u01.c001"],
                },
            ],
        )
        write_json(
            self.root / "question_concept_override.json",
            {
                "override.png": {
                    "status": "override",
                    "concept_ids": ["m1.s1.u01.c002"],
                    "source_topic_code": "7101001",
                },
                "unlinked.png": {"status": "unlinked", "reason": "검토 필요"},
            },
        )

        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                """CREATE TABLE questions (
                       id TEXT PRIMARY KEY,
                       question_filename TEXT NOT NULL,
                       grade INTEGER NOT NULL,
                       semester INTEGER NOT NULL,
                       unit_code TEXT NOT NULL,
                       unit_name TEXT NOT NULL,
                       source_topic_code TEXT NOT NULL,
                       source_topic_name TEXT NOT NULL
                   )"""
            )
            connection.executemany(
                "INSERT INTO questions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    ("normal", "normal.png", 1, 1, "01", "단원 01", "7101001", "중1 토픽"),
                    ("override", "override.png", 1, 1, "01", "단원 01", "7101001", "중1 토픽"),
                    ("unlinked", "unlinked.png", 1, 1, "01", "단원 01", "7101001", "중1 토픽"),
                    ("fallback", "fallback.png", 2, 1, "01", "단원 01", "8101001", "중2 토픽"),
                    ("related", "related.png", 3, 2, "01", "단원 01", "9301001", "중3 토픽"),
                ],
            )
            connection.commit()
        finally:
            connection.close()
        self.index = lookup_module.load_index(self.root)
        self.connection = sqlite3.connect(self.database)
        self.connection.row_factory = sqlite3.Row

    def tearDown(self):
        self.connection.close()
        self.temp.cleanup()

    def test_loads_all_grades_and_semesters(self):
        by_unit, by_id, _, overrides = self.index
        self.assertEqual(set(by_unit), {(1, 1, "01"), (2, 1, "01"), (3, 2, "01")})
        self.assertEqual(len(by_id), 4)
        self.assertEqual(set(overrides), {"override.png", "unlinked.png"})

    def test_lookup_uses_override_before_topic_mapping(self):
        override = lookup_module.lookup(self.connection, "override", self.index)
        self.assertEqual(override["match_kind"], "override")
        self.assertEqual([card["concept_id"] for card in override["concepts"]], ["m1.s1.u01.c002"])

        unlinked = lookup_module.lookup(self.connection, "unlinked", self.index)
        self.assertEqual(unlinked["match_kind"], "unlinked")
        self.assertEqual(unlinked["concepts"], [])

    def test_lookup_handles_topic_related_and_fallback_matches(self):
        normal = lookup_module.lookup(self.connection, "normal", self.index)
        self.assertEqual(normal["match_kind"], "topic")
        self.assertEqual([card["concept_id"] for card in normal["concepts"]], ["m1.s1.u01.c001"])

        fallback = lookup_module.lookup(self.connection, "fallback", self.index)
        self.assertEqual(fallback["match_kind"], "unit_fallback")
        self.assertEqual([card["concept_id"] for card in fallback["concepts"]], ["m2.s1.u01.c001"])

        related = lookup_module.lookup(self.connection, "related", self.index)
        self.assertEqual(related["match_kind"], "related")
        self.assertEqual([card["concept_id"] for card in related["concepts"]], ["m3.s2.u01.c001"])

    def test_verify_covers_all_grades_and_override_files(self):
        report = lookup_module.verify(self.connection, self.index)
        self.assertEqual(report["units"], 3)
        self.assertEqual(report["concept_cards"], 4)
        self.assertEqual(report["topics"], 3)
        self.assertEqual(report["questions"], 5)
        self.assertEqual(report["overrides"], 2)
        self.assertEqual(report["overrides_by_status"], {"override": 1, "unlinked": 1})


if __name__ == "__main__":
    unittest.main()
