-- 사진 문제의 OCR 및 유형 분류 결과 저장. 기본 문제은행/평가 데이터는 변경하지 않는다.
-- PRAGMA user_version=1은 기본 문제은행 버전으로 유지한다.
-- 이 확장의 적용 이력은 problem_schema_migrations에서 관리한다.
CREATE TABLE problem_schema_migrations (
    id TEXT PRIMARY KEY,
    schema_sha256 TEXT NOT NULL CHECK (length(schema_sha256) = 64),
    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE uploaded_problems (
    id INTEGER PRIMARY KEY,
    original_filename TEXT NOT NULL CHECK (length(original_filename) > 0),
    mime_type TEXT NOT NULL CHECK (mime_type IN (
        'image/jpeg', 'image/png', 'image/webp', 'image/heic', 'image/heif'
    )),
    image_sha256 TEXT NOT NULL CHECK (length(image_sha256) = 64),
    image_content BLOB NOT NULL CHECK (length(image_content) > 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX ix_uploaded_problems_hash ON uploaded_problems (image_sha256);

-- 같은 사진을 여러 OCR 모델로 처리한 결과를 각각 보관할 수 있다.
CREATE TABLE problem_ocr_results (
    id INTEGER PRIMARY KEY,
    uploaded_problem_id INTEGER NOT NULL REFERENCES uploaded_problems (id),
    model_name TEXT NOT NULL CHECK (length(model_name) > 0),
    model_version TEXT,
    pipeline_version TEXT NOT NULL CHECK (length(pipeline_version) > 0),
    status TEXT NOT NULL CHECK (status IN ('completed', 'failed')),
    ocr_text TEXT,
    equation_latex TEXT,
    figure_text TEXT,
    raw_response_json TEXT CHECK (raw_response_json IS NULL OR json_valid(raw_response_json)),
    error_message TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CHECK (
        (status = 'completed' AND ocr_text IS NOT NULL AND length(trim(ocr_text)) > 0 AND error_message IS NULL)
        OR (status = 'failed' AND error_message IS NOT NULL AND length(trim(error_message)) > 0)
    )
);
CREATE INDEX ix_problem_ocr_upload ON problem_ocr_results (uploaded_problem_id);

-- 정답 문항을 찾지 못해도 유형만 분류해서 저장할 수 있다.
CREATE TABLE problem_classifications (
    id INTEGER PRIMARY KEY,
    ocr_result_id INTEGER NOT NULL REFERENCES problem_ocr_results (id),
    method TEXT NOT NULL CHECK (method IN ('exact_match', 'retrieval', 'model', 'hybrid', 'manual')),
    model_name TEXT,
    model_version TEXT,
    pipeline_version TEXT NOT NULL CHECK (length(pipeline_version) > 0),
    status TEXT NOT NULL CHECK (status IN ('classified', 'unclassified', 'failed')),
    source_topic_code TEXT REFERENCES topics (source_topic_code),
    matched_question_id TEXT REFERENCES questions (id),
    confidence REAL CHECK (confidence BETWEEN 0.0 AND 1.0),
    reason TEXT,
    error_message TEXT,
    raw_response_json TEXT CHECK (raw_response_json IS NULL OR json_valid(raw_response_json)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CHECK (
        (status = 'classified' AND source_topic_code IS NOT NULL AND error_message IS NULL)
        OR (status = 'unclassified' AND source_topic_code IS NULL AND matched_question_id IS NULL
            AND confidence IS NULL AND error_message IS NULL)
        OR (status = 'failed' AND source_topic_code IS NULL AND matched_question_id IS NULL
            AND confidence IS NULL AND error_message IS NOT NULL AND length(trim(error_message)) > 0)
    )
);
CREATE INDEX ix_problem_classifications_ocr ON problem_classifications (ocr_result_id);
CREATE INDEX ix_problem_classifications_topic ON problem_classifications (source_topic_code);
CREATE INDEX ix_problem_classifications_question ON problem_classifications (matched_question_id);

CREATE TABLE problem_classification_candidates (
    classification_id INTEGER NOT NULL REFERENCES problem_classifications (id),
    rank INTEGER NOT NULL CHECK (rank > 0),
    source_topic_code TEXT NOT NULL REFERENCES topics (source_topic_code),
    matched_question_id TEXT REFERENCES questions (id),
    score REAL,
    score_name TEXT,
    reason TEXT,
    PRIMARY KEY (classification_id, rank),
    UNIQUE (classification_id, source_topic_code),
    CHECK ((score IS NULL AND score_name IS NULL)
        OR (score IS NOT NULL AND score_name IS NOT NULL AND length(trim(score_name)) > 0))
);
CREATE INDEX ix_problem_candidates_topic ON problem_classification_candidates (source_topic_code);

-- OCR 실패 결과에는 유형 분류를 연결하지 않는다.
CREATE TRIGGER trg_classification_ocr_insert
BEFORE INSERT ON problem_classifications
WHEN NOT EXISTS (SELECT 1 FROM problem_ocr_results WHERE id = NEW.ocr_result_id AND status = 'completed')
BEGIN
    SELECT RAISE(ABORT, 'classification requires completed OCR');
END;
CREATE TRIGGER trg_classification_ocr_update
BEFORE UPDATE OF ocr_result_id ON problem_classifications
WHEN NOT EXISTS (SELECT 1 FROM problem_ocr_results WHERE id = NEW.ocr_result_id AND status = 'completed')
BEGIN
    SELECT RAISE(ABORT, 'classification requires completed OCR');
END;
CREATE TRIGGER trg_ocr_status_update
BEFORE UPDATE OF status ON problem_ocr_results
WHEN NEW.status <> 'completed' AND EXISTS (SELECT 1 FROM problem_classifications WHERE ocr_result_id = OLD.id)
BEGIN
    SELECT RAISE(ABORT, 'classified OCR must remain completed');
END;

-- 선택한 문항과 유형이 서로 다른 잘못된 결과 저장을 막는다.
CREATE TRIGGER trg_classification_topic_insert
BEFORE INSERT ON problem_classifications
WHEN NEW.matched_question_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM questions WHERE id = NEW.matched_question_id AND source_topic_code = NEW.source_topic_code
)
BEGIN
    SELECT RAISE(ABORT, 'matched question topic mismatch');
END;
CREATE TRIGGER trg_classification_topic_update
BEFORE UPDATE OF matched_question_id, source_topic_code ON problem_classifications
WHEN NEW.matched_question_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM questions WHERE id = NEW.matched_question_id AND source_topic_code = NEW.source_topic_code
)
BEGIN
    SELECT RAISE(ABORT, 'matched question topic mismatch');
END;
CREATE TRIGGER trg_candidate_topic_insert
BEFORE INSERT ON problem_classification_candidates
WHEN NEW.matched_question_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM questions WHERE id = NEW.matched_question_id AND source_topic_code = NEW.source_topic_code
)
BEGIN
    SELECT RAISE(ABORT, 'candidate question topic mismatch');
END;
CREATE TRIGGER trg_candidate_topic_update
BEFORE UPDATE OF matched_question_id, source_topic_code ON problem_classification_candidates
WHEN NEW.matched_question_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM questions WHERE id = NEW.matched_question_id AND source_topic_code = NEW.source_topic_code
)
BEGIN
    SELECT RAISE(ABORT, 'candidate question topic mismatch');
END;
CREATE TRIGGER trg_question_topic_update
BEFORE UPDATE OF source_topic_code ON questions
WHEN EXISTS (
    SELECT 1 FROM problem_classifications
    WHERE matched_question_id = OLD.id AND source_topic_code <> NEW.source_topic_code
) OR EXISTS (
    SELECT 1 FROM problem_classification_candidates
    WHERE matched_question_id = OLD.id AND source_topic_code <> NEW.source_topic_code
)
BEGIN
    SELECT RAISE(ABORT, 'question topic is referenced by classification');
END;

-- 같은 유형이 여러 단원에 나타나는 경우를 보존한다. 유형 코드는 총 1,165개다.
CREATE VIEW problem_topic_catalog AS
SELECT q.grade, q.semester, q.unit_code, q.unit_name,
       t.source_topic_code, t.source_topic_name, COUNT(*) AS question_count
FROM questions q JOIN topics t ON t.source_topic_code = q.source_topic_code
GROUP BY q.grade, q.semester, q.unit_code, q.unit_name, t.source_topic_code, t.source_topic_name;

-- 분류/검색 모델이 사용할 문제 본문과 정답 유형. 정답/해설 필드는 제외한다.
CREATE VIEW question_classification_corpus AS
SELECT id, grade, semester, unit_code, unit_name, question_text, equation_latex,
       figure_text, target_skill, skill_tags, source_topic_code, source_topic_name,
       source_difficulty, question_type
FROM questions;

CREATE VIEW classified_problems AS
SELECT c.id AS classification_id, p.id AS uploaded_problem_id, p.original_filename,
       o.id AS ocr_result_id, o.ocr_text, c.method, c.model_name, c.model_version,
       c.pipeline_version, c.status, c.source_topic_code, t.source_topic_name,
       c.matched_question_id, c.confidence, c.reason, c.error_message, c.created_at
FROM problem_classifications c
JOIN problem_ocr_results o ON o.id = c.ocr_result_id
JOIN uploaded_problems p ON p.id = o.uploaded_problem_id
LEFT JOIN topics t ON t.source_topic_code = c.source_topic_code;
