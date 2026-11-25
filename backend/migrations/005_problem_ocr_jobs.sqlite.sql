-- OCR 작업 상태와 스트리밍 이벤트를 저장한다.
PRAGMA foreign_keys = ON;

CREATE TABLE problem_ocr_jobs (
    id TEXT PRIMARY KEY NOT NULL,

    submission_id INTEGER NOT NULL
        REFERENCES problem_submissions (id),

    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (
            status IN ('queued', 'running', 'completed', 'failed')
        ),

    ocr_result_id INTEGER
        REFERENCES problem_ocr_results (id),

    error_message TEXT,

    created_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    started_at TEXT,
    finished_at TEXT,

    CHECK (
        (
            status = 'queued'
            AND started_at IS NULL
            AND finished_at IS NULL
            AND ocr_result_id IS NULL
            AND error_message IS NULL
        )
        OR
        (
            status = 'running'
            AND started_at IS NOT NULL
            AND finished_at IS NULL
            AND ocr_result_id IS NULL
            AND error_message IS NULL
        )
        OR
        (
            status = 'completed'
            AND started_at IS NOT NULL
            AND finished_at IS NOT NULL
            AND ocr_result_id IS NOT NULL
            AND error_message IS NULL
        )
        OR
        (
            status = 'failed'
            AND finished_at IS NOT NULL
            AND error_message IS NOT NULL
            AND length(trim(error_message)) > 0
        )
    )
);

-- 같은 문제의 OCR이 중복 실행되는 것을 막는다.
CREATE UNIQUE INDEX ux_problem_ocr_jobs_active
ON problem_ocr_jobs (submission_id)
WHERE status IN ('queued', 'running');

CREATE TABLE problem_ocr_job_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    job_id TEXT NOT NULL
        REFERENCES problem_ocr_jobs (id),

    event_type TEXT NOT NULL
        CHECK (length(trim(event_type)) > 0),

    payload_json TEXT NOT NULL
        CHECK (
            json_valid(payload_json)
            AND json_type(payload_json) = 'object'
        ),

    created_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX ix_problem_ocr_job_events_job
ON problem_ocr_job_events (job_id, id);

-- OCR 작업은 사진 문제의 입력 버전이 만들어지기 전에 대기로 시작한다.
CREATE TRIGGER trg_ocr_job_submission_insert
BEFORE INSERT ON problem_ocr_jobs
WHEN NEW.status <> 'queued'
    OR NOT EXISTS (
        SELECT 1
        FROM problem_submissions
        WHERE id = NEW.submission_id
          AND input_kind = 'photo'
    )
    OR EXISTS (
        SELECT 1
        FROM problem_input_versions
        WHERE submission_id = NEW.submission_id
    )
BEGIN
    SELECT RAISE(
        ABORT,
        'OCR job requires a photo awaiting OCR and queued status'
    );
END;

-- 완료·실패 결과를 연결할 때 같은 사진의 OCR인지 검사한다.
CREATE TRIGGER trg_ocr_job_result_update
BEFORE UPDATE OF ocr_result_id ON problem_ocr_jobs
WHEN NEW.ocr_result_id IS NOT NULL
AND NOT EXISTS (
    SELECT 1
    FROM problem_submissions s
    JOIN problem_ocr_results o
        ON o.uploaded_problem_id = s.uploaded_problem_id
    WHERE s.id = NEW.submission_id
      AND o.id = NEW.ocr_result_id
)
BEGIN
    SELECT RAISE(
        ABORT,
        'OCR result belongs to another submission'
    );
END;