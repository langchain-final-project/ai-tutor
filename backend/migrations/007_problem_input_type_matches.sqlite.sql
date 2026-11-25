CREATE TABLE IF NOT EXISTS problem_input_type_matches (
    id INTEGER PRIMARY KEY,
    input_version_id INTEGER NOT NULL REFERENCES problem_input_versions(id),
    scope_key TEXT NOT NULL,
    input_sha256 TEXT NOT NULL CHECK(length(input_sha256) = 64),
    result_json TEXT NOT NULL CHECK(json_valid(result_json)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS ix_input_type_match_version
ON problem_input_type_matches(input_version_id, scope_key, id);
