-- 기존 유형과 개별 튜터 시도를 연결하며 개념 숙달 점수는 만들지 않는다.
CREATE TABLE learning_paths (
    id INTEGER PRIMARY KEY,
    source_session_id INTEGER NOT NULL UNIQUE REFERENCES tutor_sessions(id),
    type_code TEXT NOT NULL CHECK(type_code IN ('7103047','7103048')),
    type_name TEXT NOT NULL,
    phase TEXT NOT NULL DEFAULT 'remediation'
        CHECK(phase IN ('remediation','practice','independent','review_due','review')),
    revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
    current_activity_id INTEGER REFERENCES learning_activities(id),
    review_due_at TEXT,
    review_count INTEGER NOT NULL DEFAULT 0 CHECK(review_count >= 0),
    policy_version TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE learning_activities (
    id INTEGER PRIMARY KEY,
    path_id INTEGER NOT NULL REFERENCES learning_paths(id),
    session_id INTEGER UNIQUE REFERENCES tutor_sessions(id),
    catalog_item_id TEXT NOT NULL,
    source_question_id TEXT NOT NULL,
    question_text TEXT NOT NULL,
    target_text TEXT NOT NULL,
    mode TEXT NOT NULL CHECK(mode IN ('practice','independent','review')),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','completed','needs_support')),
    is_new_question INTEGER NOT NULL CHECK(is_new_question IN (0,1)),
    selection_reason TEXT NOT NULL,
    outcome_json TEXT NOT NULL CHECK(json_valid(outcome_json)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    completed_at TEXT
);

CREATE INDEX ix_learning_activity_path ON learning_activities(path_id,id);

CREATE TRIGGER trg_learning_current_activity
BEFORE UPDATE OF current_activity_id ON learning_paths
WHEN NEW.current_activity_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM learning_activities a WHERE a.id=NEW.current_activity_id AND a.path_id=NEW.id
)
BEGIN
    SELECT RAISE(ABORT, 'Learning activity belongs to another path');
END;

CREATE TRIGGER trg_learning_activity_owner
BEFORE UPDATE OF session_id ON learning_activities
WHEN NEW.session_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM learning_paths p
    JOIN tutor_sessions original ON original.id=p.source_session_id
    JOIN problem_submissions source ON source.id=original.submission_id
    JOIN tutor_sessions attempt ON attempt.id=NEW.session_id
    JOIN problem_submissions student ON student.id=attempt.submission_id
    WHERE p.id=NEW.path_id AND student.owner_id=source.owner_id
)
BEGIN
    SELECT RAISE(ABORT, 'Learning session belongs to another student');
END;
