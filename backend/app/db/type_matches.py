"""유형 후보와 선택을 확인된 학생 입력 버전에 연결해 보관한다."""

import hashlib
import json
from pathlib import Path


SCOPE_KEY = "middle1-semester1-unit03"


def ensure_type_match_schema(connection):
    schema = (Path(__file__).resolve().parents[2] / "migrations/007_problem_input_type_matches.sqlite.sql").read_text(encoding="utf-8")
    for statement in schema.split(";"):
        if statement.strip():
            connection.execute(statement)


def load_type_match(connection, input_version_id):
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='problem_input_type_matches'").fetchone():
        return None
    row = connection.execute(
        "SELECT result_json FROM problem_input_type_matches WHERE input_version_id=? AND scope_key=? ORDER BY id DESC LIMIT 1",
        (input_version_id, SCOPE_KEY),
    ).fetchone()
    return json.loads(row[0]) if row else None


def save_type_match(connection, latest, result):
    result = {**result, "input_version_number": latest["version_number"]}
    connection.execute(
        "INSERT INTO problem_input_type_matches(input_version_id,scope_key,input_sha256,result_json) VALUES(?,?,?,?)",
        (latest["id"], SCOPE_KEY, hashlib.sha256(latest["content_text"].encode()).hexdigest(),
         json.dumps(result, ensure_ascii=False, allow_nan=False)),
    )
    return result
