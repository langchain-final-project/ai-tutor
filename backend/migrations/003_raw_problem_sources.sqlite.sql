-- 기존 공유 DB가 아닌 별도의 로컬 원천 연결 DB에 적용한다.
PRAGMA user_version = 1;

CREATE TABLE source_archives (
    archive_key TEXT PRIMARY KEY,
    split TEXT NOT NULL CHECK (split IN ('Training', 'Validation')),
    grade INTEGER NOT NULL CHECK (grade BETWEEN 1 AND 3),
    role TEXT NOT NULL CHECK (role IN ('question_image', 'answer_image', 'question_label', 'answer_label')),
    relative_path TEXT NOT NULL UNIQUE,
    bytes INTEGER NOT NULL CHECK (bytes > 0),
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    UNIQUE (split, grade, role)
);

CREATE TABLE raw_problems (
    split TEXT NOT NULL CHECK (split IN ('Training', 'Validation')),
    question_id TEXT NOT NULL,
    bank_question_id TEXT,
    grade INTEGER NOT NULL,
    semester INTEGER NOT NULL,
    unit_code TEXT NOT NULL,
    source_topic_code TEXT NOT NULL,
    source_topic_name TEXT NOT NULL,
    question_type TEXT NOT NULL,
    difficulty INTEGER NOT NULL,
    sector1 TEXT,
    sector2 TEXT,
    question_step TEXT,
    question_text TEXT NOT NULL,
    figure_text TEXT,
    answer_text TEXT NOT NULL,
    question_metadata_json TEXT NOT NULL CHECK (json_valid(question_metadata_json)),
    raw_question_json TEXT NOT NULL CHECK (json_valid(raw_question_json)),
    raw_answer_json TEXT NOT NULL CHECK (json_valid(raw_answer_json)),
    reserved_ocr50 INTEGER NOT NULL CHECK (reserved_ocr50 IN (0, 1)),
    body_sha256 TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    template_sha256 TEXT NOT NULL,
    PRIMARY KEY (split, question_id)
);
CREATE INDEX ix_raw_problem_bank ON raw_problems (bank_question_id);
CREATE INDEX ix_raw_problem_topic ON raw_problems (split, grade, source_topic_code);
CREATE INDEX ix_raw_problem_body ON raw_problems (body_sha256);
CREATE INDEX ix_raw_problem_input ON raw_problems (input_sha256);
CREATE INDEX ix_raw_problem_template ON raw_problems (template_sha256);

CREATE TABLE source_assets (
    split TEXT NOT NULL,
    question_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('question', 'answer')),
    archive_key TEXT NOT NULL REFERENCES source_archives (archive_key),
    member TEXT NOT NULL,
    filename TEXT NOT NULL,
    bytes INTEGER NOT NULL CHECK (bytes > 0),
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    width INTEGER NOT NULL CHECK (width > 0),
    height INTEGER NOT NULL CHECK (height > 0),
    PRIMARY KEY (split, question_id, kind),
    UNIQUE (archive_key, member),
    FOREIGN KEY (split, question_id) REFERENCES raw_problems (split, question_id)
);

CREATE VIEW question_regions AS
SELECT p.split, p.question_id, CAST(o.key AS INTEGER) AS block_index,
       CAST(b.key AS INTEGER) AS region_index,
       json_extract(b.value, '$.type') AS region_type,
       json_extract(b.value, '$.bbox') AS bbox_json,
       json_extract(b.value, '$.text') AS text
FROM raw_problems p, json_each(p.raw_question_json, '$.OCR_info') o,
     json_each(o.value, '$.question_bbox') b;

CREATE VIEW answer_regions AS
SELECT p.split, p.question_id, CAST(o.key AS INTEGER) AS block_index,
       CAST(b.key AS INTEGER) AS region_index,
       json_extract(b.value, '$.type') AS region_type,
       json_extract(b.value, '$.bbox') AS bbox_json,
       json_extract(b.value, '$.text') AS text
FROM raw_problems p, json_each(p.raw_answer_json, '$.answer_info') o,
     json_each(o.value, '$.answer_bbox') b;

CREATE VIEW raw_topic_catalog AS
SELECT split, grade, semester, unit_code, source_topic_code, source_topic_name,
       sector1, sector2, question_step, COUNT(*) AS question_count
FROM raw_problems
GROUP BY split, grade, semester, unit_code, source_topic_code, source_topic_name,
         sector1, sector2, question_step;

-- 공식 Validation과 공통 OCR 50문항을 학습에 섞지 않는다.
-- 숫자만 가린 본문 틀이 공통 50문항과 같은 Training 문항도 제외한다.
CREATE VIEW classifier_training_pool AS
SELECT p.* FROM raw_problems p
WHERE p.split = 'Training' AND p.reserved_ocr50 = 0
  AND NOT EXISTS (
      SELECT 1 FROM raw_problems held
      WHERE held.reserved_ocr50 = 1 AND held.template_sha256 = p.template_sha256
  );
