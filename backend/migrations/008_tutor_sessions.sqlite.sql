-- 확인된 문제 버전과 학생의 도움·재시도를 연결한다.
CREATE TABLE tutor_sessions (
    id INTEGER PRIMARY KEY,
    submission_id INTEGER NOT NULL REFERENCES problem_submissions(id),
    input_version_id INTEGER NOT NULL REFERENCES problem_input_versions(id),
    subject TEXT NOT NULL CHECK(length(trim(subject)) > 0),
    target_text TEXT NOT NULL CHECK(length(trim(target_text)) > 0),
    target_sha256 TEXT NOT NULL,
    problem_json TEXT NOT NULL CHECK(json_valid(problem_json)),
    state_json TEXT NOT NULL CHECK(json_valid(state_json)),
    revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(input_version_id, subject, target_sha256)
);

CREATE TABLE tutor_turns (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES tutor_sessions(id),
    request_id TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    expected_revision INTEGER NOT NULL CHECK(expected_revision >= 0),
    request_json TEXT NOT NULL CHECK(json_valid(request_json)),
    status TEXT NOT NULL CHECK(status IN ('running','completed','failed','stale')),
    lease_token TEXT NOT NULL,
    lease_expires_at REAL NOT NULL,
    response_json TEXT CHECK(response_json IS NULL OR json_valid(response_json)),
    state_after_json TEXT CHECK(state_after_json IS NULL OR json_valid(state_after_json)),
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    completed_at TEXT,
    UNIQUE(session_id, request_id),
    CHECK(status <> 'completed' OR (response_json IS NOT NULL AND state_after_json IS NOT NULL))
);

-- 같은 세션의 두 요청이 같은 학습 상태에서 동시에 실행되지 않게 한다.
CREATE UNIQUE INDEX ix_tutor_one_running ON tutor_turns(session_id) WHERE status='running';
CREATE INDEX ix_tutor_turn_history ON tutor_turns(session_id,id);
CREATE INDEX ix_tutor_submission ON tutor_sessions(submission_id,id);

CREATE TRIGGER trg_tutor_input_submission
BEFORE INSERT ON tutor_sessions
WHEN NOT EXISTS (SELECT 1 FROM problem_input_versions v
                 WHERE v.id=NEW.input_version_id AND v.submission_id=NEW.submission_id)
BEGIN
    SELECT RAISE(ABORT, 'Tutor input belongs to another submission');
END;

CREATE TRIGGER trg_tutor_source_immutable
BEFORE UPDATE OF submission_id,input_version_id,subject,target_text,target_sha256,problem_json ON tutor_sessions
BEGIN
    SELECT RAISE(ABORT, 'Create a new tutor session for changed input');
END;
