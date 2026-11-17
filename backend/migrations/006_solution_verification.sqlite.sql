-- 학생 풀이·검증·표시 확인을 문제 입력 버전에 연결한다.
PRAGMA foreign_keys = ON;

CREATE UNIQUE INDEX ux_input_versions_id_submission
ON problem_input_versions (id, submission_id);

CREATE TABLE student_work_versions (
    id INTEGER PRIMARY KEY,
    submission_id INTEGER NOT NULL REFERENCES problem_submissions (id),
    problem_input_version_id INTEGER NOT NULL,
    version_number INTEGER NOT NULL CHECK (version_number > 0),
    steps_json TEXT NOT NULL CHECK (
        json_valid(steps_json) AND json_type(steps_json) = 'array'
        AND json_array_length(steps_json) <= 100
    ),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    confirmed_at TEXT,
    FOREIGN KEY (problem_input_version_id, submission_id)
        REFERENCES problem_input_versions (id, submission_id),
    UNIQUE (problem_input_version_id, version_number),
    UNIQUE (id, problem_input_version_id)
);

CREATE TRIGGER trg_work_version_insert
BEFORE INSERT ON student_work_versions
BEGIN
    SELECT CASE WHEN NEW.version_number <> (
        SELECT COALESCE(MAX(version_number), 0) + 1 FROM student_work_versions
        WHERE problem_input_version_id = NEW.problem_input_version_id
    ) THEN RAISE(ABORT, 'Work versions must be added in order') END;
    SELECT CASE WHEN NEW.confirmed_at IS NOT NULL
        THEN RAISE(ABORT, 'New work must start unconfirmed') END;
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM problem_input_versions newer
        JOIN problem_input_versions original ON original.id = NEW.problem_input_version_id
        WHERE newer.submission_id = original.submission_id
          AND newer.version_number > original.version_number
    ) THEN RAISE(ABORT, 'Work requires the latest problem input') END;
END;

CREATE TRIGGER trg_work_content_immutable
BEFORE UPDATE OF id, submission_id, problem_input_version_id,
    version_number, steps_json, created_at ON student_work_versions
BEGIN
    SELECT RAISE(ABORT, 'Create a new work version instead of updating');
END;

CREATE TRIGGER trg_work_confirmation_update
BEFORE UPDATE OF confirmed_at ON student_work_versions
WHEN OLD.confirmed_at IS NOT NULL OR NEW.confirmed_at IS NULL
    OR length(trim(NEW.confirmed_at)) = 0
    OR EXISTS (
        SELECT 1 FROM student_work_versions newer
        WHERE newer.problem_input_version_id = OLD.problem_input_version_id
          AND newer.version_number > OLD.version_number
    )
    OR NOT EXISTS (
        SELECT 1 FROM problem_input_versions current
        WHERE current.id = OLD.problem_input_version_id
          AND current.confirmed_at IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM problem_input_versions newer
              WHERE newer.submission_id = current.submission_id
                AND newer.version_number > current.version_number
          )
    )
BEGIN
    SELECT RAISE(ABORT, 'Confirm only current work for a confirmed problem');
END;

CREATE TABLE solution_verifications (
    id INTEGER PRIMARY KEY,
    submission_id INTEGER NOT NULL REFERENCES problem_submissions (id),
    problem_input_version_id INTEGER NOT NULL,
    work_version_id INTEGER,
    problem_version_number INTEGER NOT NULL CHECK (problem_version_number > 0),
    work_version_number INTEGER NOT NULL CHECK (work_version_number >= 0),
    input_fingerprint TEXT NOT NULL CHECK (length(input_fingerprint) = 64),
    verifier_version TEXT NOT NULL,
    problem_json TEXT NOT NULL CHECK (json_valid(problem_json) AND json_type(problem_json) = 'object'),
    work_json TEXT NOT NULL CHECK (json_valid(work_json) AND json_type(work_json) = 'object'),
    evaluation_json TEXT NOT NULL CHECK (json_valid(evaluation_json) AND json_type(evaluation_json) = 'object'),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (problem_input_version_id, submission_id)
        REFERENCES problem_input_versions (id, submission_id),
    FOREIGN KEY (work_version_id, problem_input_version_id)
        REFERENCES student_work_versions (id, problem_input_version_id),
    CHECK ((work_version_id IS NULL AND work_version_number = 0)
           OR (work_version_id IS NOT NULL AND work_version_number > 0))
);

CREATE TRIGGER trg_verification_insert
BEFORE INSERT ON solution_verifications
WHEN NOT EXISTS (
    SELECT 1 FROM problem_input_versions p
    WHERE p.id = NEW.problem_input_version_id
      AND p.version_number = NEW.problem_version_number
) OR (NEW.work_version_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM student_work_versions w
    WHERE w.id = NEW.work_version_id AND w.version_number = NEW.work_version_number
))
BEGIN
    SELECT RAISE(ABORT, 'Verification version references do not match');
END;

CREATE TRIGGER trg_verification_immutable
BEFORE UPDATE ON solution_verifications
BEGIN
    SELECT RAISE(ABORT, 'Verification records are immutable');
END;

CREATE TABLE feedback_view_events (
    id INTEGER PRIMARY KEY,
    verification_result_id INTEGER NOT NULL UNIQUE REFERENCES solution_verifications (id),
    feedback_json TEXT NOT NULL CHECK (json_valid(feedback_json) AND json_type(feedback_json) = 'object'),
    viewed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TRIGGER trg_feedback_event_immutable
BEFORE UPDATE ON feedback_view_events
BEGIN
    SELECT RAISE(ABORT, 'Feedback view records are immutable');
END;
