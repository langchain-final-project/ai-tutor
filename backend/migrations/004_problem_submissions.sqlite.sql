-- 질문 제출 정보를 저장한다.
PRAGMA foreign_keys = ON;

CREATE TABLE problem_submissions (
    id INTEGER PRIMARY KEY,

    owner_id TEXT NOT NULL
        CHECK (length(trim(owner_id)) > 0),

    input_kind TEXT NOT NULL
        CHECK (input_kind IN ('photo', 'text')),

    uploaded_problem_id INTEGER UNIQUE
        REFERENCES uploaded_problems (id),

    created_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    CHECK (
        (input_kind = 'photo' AND uploaded_problem_id IS NOT NULL)
        OR
        (input_kind = 'text' AND uploaded_problem_id IS NULL)
    )
);

-- OCR에서 가져온 내용과 학생이 수정한 내용을 버전별로 보관
CREATE TABLE problem_input_versions (
    id INTEGER PRIMARY KEY,

    submission_id INTEGER NOT NULL
        REFERENCES problem_submissions (id),

    version_number INTEGER NOT NULL
        CHECK (version_number > 0),

    source_ocr_result_id INTEGER
        REFERENCES problem_ocr_results (id),

    origin TEXT NOT NULL
        CHECK (origin IN ('ocr', 'student')),

    content_text TEXT NOT NULL
        CHECK (length(trim(content_text)) > 0),

    created_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    confirmed_at TEXT,

    UNIQUE (submission_id, version_number),

    CHECK (
        origin = 'student'
        OR source_ocr_result_id IS NOT NULL
    )
);

-- 입력 버전의 OCR은 같은 문제에 제출된 사진에서 나온 결과여야 한다.
CREATE TRIGGER trg_input_version_ocr_insert
BEFORE INSERT ON problem_input_versions
WHEN NEW.source_ocr_result_id IS NOT NULL
AND NOT EXISTS (
    SELECT 1
    FROM problem_submissions s
    JOIN problem_ocr_results o
        ON o.uploaded_problem_id = s.uploaded_problem_id
    WHERE s.id = NEW.submission_id
      AND o.id = NEW.source_ocr_result_id
)
BEGIN
    SELECT RAISE(ABORT, 'OCR result belongs to another submission');
END;

-- 내용을 고칠 때는 기존 행을 수정하지 않고 새 버전을 추가한다.
CREATE TRIGGER trg_input_version_content_update
BEFORE UPDATE OF
    id, submission_id, version_number, source_ocr_result_id,
    origin, content_text, created_at
ON problem_input_versions
BEGIN
    SELECT RAISE(ABORT, 'Create a new input version instead of updating');
END;

-- 다른 사진을 제출하려면 새 문제 제출 기록을 만든다.
CREATE TRIGGER trg_submission_photo_update
BEFORE UPDATE OF uploaded_problem_id ON problem_submissions
WHEN NEW.uploaded_problem_id IS NOT OLD.uploaded_problem_id
BEGIN
    SELECT RAISE(ABORT, 'Create a new submission for another photo');
END;

-- 입력 버전에서 참조 중인 OCR의 사진 연결을 보호한다.
CREATE TRIGGER trg_referenced_ocr_photo_update
BEFORE UPDATE OF uploaded_problem_id ON problem_ocr_results
WHEN NEW.uploaded_problem_id IS NOT OLD.uploaded_problem_id
AND EXISTS (
    SELECT 1
    FROM problem_input_versions
    WHERE source_ocr_result_id = OLD.id
)
BEGIN
    SELECT RAISE(ABORT, 'Referenced OCR photo cannot be changed');
END;

-- 버전은 1부터 순서대로 추가하고, 확인 완료는 별도로 처리한다.
CREATE TRIGGER trg_input_version_initial_state
BEFORE INSERT ON problem_input_versions
BEGIN
    SELECT CASE
        WHEN NEW.version_number <> (
            SELECT COALESCE(MAX(version_number), 0) + 1
            FROM problem_input_versions
            WHERE submission_id = NEW.submission_id
        )
        THEN RAISE(ABORT, 'Input versions must be added in order')
    END;

    SELECT CASE
        WHEN NEW.confirmed_at IS NOT NULL
        THEN RAISE(ABORT, 'New input versions must start unconfirmed')
    END;
END;

-- 확인 시각은 최신의 미확인 버전에 한 번만 기록한다.
CREATE TRIGGER trg_input_version_confirmation_update
BEFORE UPDATE OF confirmed_at ON problem_input_versions
WHEN OLD.confirmed_at IS NOT NULL
    OR NEW.confirmed_at IS NULL
    OR length(trim(NEW.confirmed_at)) = 0
    OR EXISTS (
        SELECT 1
        FROM problem_input_versions
        WHERE submission_id = OLD.submission_id
          AND version_number > OLD.version_number
    )
BEGIN
    SELECT RAISE(
        ABORT,
        'Only the latest unconfirmed version can be confirmed'
    );
END;
