-- 기존 경로와 활동 ID를 보존한다. 지원 유형은 추천 정책이 결정한다.
CREATE TABLE learning_paths_v2 (
    id INTEGER PRIMARY KEY,
    source_session_id INTEGER NOT NULL UNIQUE REFERENCES tutor_sessions(id),
    type_code TEXT NOT NULL CHECK(length(trim(type_code)) > 0),
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

INSERT INTO learning_paths_v2 (
    id, source_session_id, type_code, type_name, phase, revision, current_activity_id,
    review_due_at, review_count, policy_version, created_at, updated_at
)
SELECT id, source_session_id, type_code, type_name, phase, revision, current_activity_id,
    review_due_at, review_count, policy_version, created_at, updated_at
FROM learning_paths;

DROP TRIGGER trg_learning_activity_owner;
DROP TABLE learning_paths;
ALTER TABLE learning_paths_v2 RENAME TO learning_paths;

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
