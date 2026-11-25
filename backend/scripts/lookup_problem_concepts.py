"""Look up concept cards for a problem in the existing SQLite problem bank."""

import argparse
import json
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / "data/problem_bank/problem_bank.seed.sqlite3"
DEFAULT_CONCEPTS = ROOT / "data/concept_bank"
UNIT_FIELDS = ("grade", "semester", "unit_code")
TOPIC_FIELDS = (*UNIT_FIELDS, "source_topic_code")
MATCH_KINDS = {"topic", "related", "unit_fallback"}
OVERRIDE_STATUSES = {"override", "unlinked"}
GRADE_DIRECTORIES = ("m1", "m2", "m3")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def key_for(row, fields):
    try:
        return tuple(row[field] for field in fields)
    except (KeyError, TypeError) as error:
        raise ValueError(f"Missing required fields: {fields}") from error


def cards_in(document):
    return [
        card
        for section in document["sections"]
        for subsection in section["subsections"]
        for card in subsection["concepts"]
    ]


def load_overrides(root, cards_by_id):
    raw_overrides = read_json(root / "question_concept_override.json")
    if not isinstance(raw_overrides, dict):
        raise ValueError("Question concept overrides must be an object keyed by question filename")

    overrides = {}
    for question_filename, override in raw_overrides.items():
        if not isinstance(override, dict):
            raise ValueError(f"Invalid question override: {question_filename}")
        status = override.get("status")
        concept_ids = override.get("concept_ids", [])
        if status not in OVERRIDE_STATUSES:
            raise ValueError(f"Invalid question override status: {question_filename}")
        if not isinstance(concept_ids, list) or len(set(concept_ids)) != len(concept_ids):
            raise ValueError(f"Invalid concept IDs in question override: {question_filename}")
        if (status == "override") != bool(concept_ids):
            raise ValueError(f"Invalid override status or concept IDs: {question_filename}")
        if any(concept_id not in cards_by_id for concept_id in concept_ids):
            raise ValueError(f"Missing concept reference in question override: {question_filename}")
        overrides[question_filename] = override
    return overrides


def load_index(root):
    cards_by_unit = {}
    cards_by_id = {}
    for grade_directory in GRADE_DIRECTORIES:
        for path in sorted((root / grade_directory).glob("*_concepts.json")):
            doc = read_json(path)
            unit = key_for(doc, UNIT_FIELDS)
            if unit in cards_by_unit:
                raise ValueError(f"Duplicate concept unit: {unit}")
            cards = cards_in(doc)
            if not cards:
                raise ValueError(f"No concept cards: {path}")
            cards_by_unit[unit] = cards
            for card in cards:
                concept_id = card.get("concept_id")
                if not isinstance(concept_id, str) or not concept_id:
                    raise ValueError(f"Missing concept ID: {path}")
                if concept_id in cards_by_id:
                    raise ValueError(f"Duplicate concept ID: {concept_id}")
                cards_by_id[concept_id] = (unit, card)

    mapping = {}
    raw_mapping = read_json(root / "topic_concept_map.json")
    if not isinstance(raw_mapping, list):
        raise ValueError("Topic concept mapping must be a list")
    for row in raw_mapping:
        if not isinstance(row, dict):
            raise ValueError("Invalid topic concept mapping row")
        key = key_for(row, TOPIC_FIELDS)
        unit = key[:-1]
        kind = row.get("match_kind")
        ids = row.get("concept_ids")
        if key in mapping or unit not in cards_by_unit:
            raise ValueError(f"Duplicate mapping or missing concept unit: {key}")
        if not isinstance(ids, list) or kind not in MATCH_KINDS or (kind == "unit_fallback") != (not ids):
            raise ValueError(f"Invalid match kind or concept IDs: {key}")
        if len(set(ids)) != len(ids):
            raise ValueError(f"Duplicate concept references: {key}")
        if any(concept_id not in cards_by_id or cards_by_id[concept_id][0] != unit for concept_id in ids):
            raise ValueError(f"Missing or out-of-unit concept reference: {key}")
        mapping[key] = row
    overrides = load_overrides(root, cards_by_id)
    return cards_by_unit, cards_by_id, mapping, overrides


def lookup(db, question_id, index):
    by_unit, by_id, mapping, overrides = index
    question = db.execute(
        """SELECT id, question_filename, grade, semester, unit_code, unit_name,
                  source_topic_code, source_topic_name
           FROM questions WHERE id = ?""",
        (question_id,),
    ).fetchone()
    if question is None:
        raise ValueError(f"Question not found: {question_id}")
    unit = key_for(question, UNIT_FIELDS)
    override = overrides.get(question["question_filename"])
    if override:
        kind = override["status"]
        cards = [by_id[concept_id][1] for concept_id in override.get("concept_ids", [])]
    elif unit not in by_unit:
        kind, cards = "outside_scope", []
    else:
        match = mapping.get(key_for(question, TOPIC_FIELDS))
        if match and match["concept_ids"]:
            kind = match["match_kind"]
            cards = [by_id[concept_id][1] for concept_id in match["concept_ids"]]
        else:
            kind, cards = "unit_fallback", by_unit[unit]
    return {"question": dict(question), "match_kind": kind, "concepts": cards}


def verify(db, index):
    by_unit, by_id, mapping, overrides = index
    groups = db.execute(
        """SELECT grade, semester, unit_code, source_topic_code,
                  source_topic_name, COUNT(*) AS question_count
           FROM questions
           GROUP BY grade, semester, unit_code, source_topic_code, source_topic_name"""
    ).fetchall()
    actual = {key_for(row, TOPIC_FIELDS): row for row in groups}
    if actual.keys() != mapping.keys() or len(actual) != len(groups):
        raise ValueError("Concept mappings do not match the DB's M1 semester 1 topic list")
    topics, questions = Counter(), Counter()
    for key, row in actual.items():
        match = mapping[key]
        if row["source_topic_name"] != match["source_topic_name"]:
            raise ValueError(f"Topic name differs from DB: {key}")
        kind = match["match_kind"]
        topics[kind] += 1
        questions[kind] += row["question_count"]

    question_topics = {
        row["question_filename"]: row["source_topic_code"]
        for row in db.execute("SELECT question_filename, source_topic_code FROM questions").fetchall()
    }
    if len(question_topics) != db.execute("SELECT COUNT(*) FROM questions").fetchone()[0]:
        raise ValueError("Question filenames must be unique for concept overrides")
    for question_filename, override in overrides.items():
        if question_filename not in question_topics:
            raise ValueError(f"Override question not found: {question_filename}")
        override_topic = override.get("source_topic_code")
        if override_topic is not None and override_topic != question_topics[question_filename]:
            raise ValueError(f"Override topic differs from DB: {question_filename}")

    return {
        "units": len(by_unit),
        "concept_cards": len(by_id),
        "topics": len(actual),
        "questions": sum(questions.values()),
        "topics_by_match_kind": topics,
        "questions_by_match_kind": questions,
        "overrides": len(overrides),
        "overrides_by_status": Counter(override["status"] for override in overrides.values()),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--question-id")
    action.add_argument("--verify", action="store_true")
    parser.add_argument("--database", type=Path, default=DEFAULT_DB)
    parser.add_argument("--concept-root", type=Path, default=DEFAULT_CONCEPTS)
    args = parser.parse_args()
    try:
        index = load_index(args.concept_root)
        with closing(sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            result = verify(db, index) if args.verify else lookup(db, args.question_id, index)
    except (OSError, sqlite3.Error, ValueError) as error:
        parser.exit(1, f"{error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
