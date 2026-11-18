-- 확인한 사진 문제 원문을 보존하고, 풀이용 등식 문제의 출처를 연결한다.
CREATE TABLE photo_equation_sources (
    submission_id INTEGER PRIMARY KEY
        REFERENCES problem_submissions (id),
    source_submission_id INTEGER NOT NULL
        REFERENCES problem_submissions (id),
    source_input_version_id INTEGER NOT NULL,
    created_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (source_input_version_id, source_submission_id)
        REFERENCES problem_input_versions (id, submission_id)
);

CREATE TRIGGER trg_photo_equation_source_insert
BEFORE INSERT ON photo_equation_sources
WHEN NOT EXISTS (
    SELECT 1
    FROM problem_submissions target
    JOIN problem_submissions source ON source.id = NEW.source_submission_id
    JOIN problem_input_versions version
        ON version.id = NEW.source_input_version_id
        AND version.submission_id = source.id
    WHERE target.id = NEW.submission_id
        AND target.input_kind = 'text'
        AND source.input_kind = 'photo'
        AND target.owner_id = source.owner_id
        AND version.confirmed_at IS NOT NULL
)
BEGIN
    SELECT RAISE(ABORT, 'Equation source requires a confirmed photo owned by the same student');
END;

CREATE TRIGGER trg_photo_equation_source_immutable
BEFORE UPDATE ON photo_equation_sources
BEGIN
    SELECT RAISE(ABORT, 'Photo equation sources are immutable');
END;
