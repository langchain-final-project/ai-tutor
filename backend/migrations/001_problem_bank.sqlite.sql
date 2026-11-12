-- SQLite 전용 문항/공통 OCR 평가 데이터 스키마. 기존 API에는 자동 적용하지 않는다.
PRAGMA user_version = 1;

CREATE TABLE dataset_sources (
    name TEXT PRIMARY KEY,
    archive_sha256 TEXT NOT NULL CHECK (length(archive_sha256) = 64),
    archive_bytes INTEGER NOT NULL CHECK (archive_bytes > 0)
);

CREATE TABLE units (
    grade INTEGER NOT NULL CHECK (grade BETWEEN 1 AND 3),
    semester INTEGER NOT NULL CHECK (semester IN (1, 2)),
    unit_code TEXT NOT NULL,
    unit_name TEXT NOT NULL,
    PRIMARY KEY (grade, semester, unit_code),
    UNIQUE (grade, semester, unit_code, unit_name)
);

CREATE TABLE topics (
    source_topic_code TEXT PRIMARY KEY,
    source_topic_name TEXT NOT NULL,
    UNIQUE (source_topic_code, source_topic_name)
);

CREATE TABLE questions (
    id TEXT PRIMARY KEY,
    grade INTEGER NOT NULL,
    semester INTEGER NOT NULL,
    unit_code TEXT NOT NULL,
    unit_name TEXT NOT NULL,
    source_id TEXT NOT NULL,
    subquestion_number INTEGER CHECK (subquestion_number > 0),
    source_topic_code TEXT NOT NULL,
    source_topic_name TEXT NOT NULL,
    question_text TEXT NOT NULL CHECK (length(question_text) > 0),
    equation_latex TEXT,
    answer TEXT,
    target_skill TEXT NOT NULL,
    skill_tags TEXT NOT NULL CHECK (json_valid(skill_tags) AND json_type(skill_tags) = 'array'),
    question_type TEXT NOT NULL CHECK (question_type IN ('선택형', '단답형')),
    source_difficulty INTEGER NOT NULL CHECK (source_difficulty IN (1, 2, 3)),
    question_filename TEXT NOT NULL CHECK (question_filename = id),
    answer_filename TEXT NOT NULL,
    figure_text TEXT,
    answer_text TEXT NOT NULL,
    answer_box_texts TEXT NOT NULL CHECK (json_valid(answer_box_texts) AND json_type(answer_box_texts) = 'array'),
    review_status TEXT NOT NULL CHECK (review_status IN ('verified', 'source_imported')),
    source_member TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    FOREIGN KEY (grade, semester, unit_code, unit_name)
        REFERENCES units (grade, semester, unit_code, unit_name),
    FOREIGN KEY (source_topic_code, source_topic_name)
        REFERENCES topics (source_topic_code, source_topic_name)
);
CREATE INDEX ix_questions_unit ON questions (grade, semester, unit_code);
CREATE INDEX ix_questions_topic ON questions (source_topic_code);
CREATE INDEX ix_questions_difficulty ON questions (source_difficulty);
CREATE INDEX ix_questions_target_skill ON questions (target_skill);

-- 비교용 이미지 100개만 실제 바이트를 보관한다. 나머지 문항은 원천 파일명만 있다.
CREATE TABLE benchmark_assets (
    source_member TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    mime_type TEXT NOT NULL CHECK (mime_type = 'image/png'),
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    content BLOB NOT NULL CHECK (length(content) > 0)
);

CREATE TABLE benchmark_sets (
    id TEXT PRIMARY KEY,
    item_count INTEGER NOT NULL CHECK (item_count > 0),
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    source_name TEXT NOT NULL REFERENCES dataset_sources (name)
);

CREATE TABLE benchmark_items (
    benchmark_id TEXT NOT NULL REFERENCES benchmark_sets (id),
    position INTEGER NOT NULL CHECK (position > 0),
    question_id TEXT NOT NULL REFERENCES questions (id),
    recognition_type TEXT NOT NULL,
    selection_reason TEXT NOT NULL,
    source_bank_file TEXT NOT NULL,
    question_asset TEXT NOT NULL REFERENCES benchmark_assets (source_member),
    answer_asset TEXT NOT NULL REFERENCES benchmark_assets (source_member),
    selection_metadata TEXT NOT NULL CHECK (json_valid(selection_metadata) AND json_type(selection_metadata) = 'object'),
    PRIMARY KEY (benchmark_id, position),
    UNIQUE (benchmark_id, question_id)
);
CREATE INDEX ix_benchmark_items_question ON benchmark_items (question_id);

-- 이 뷰를 조회할 때도 반드시 ORDER BY position을 지정한다.
CREATE VIEW ocr50_questions AS
SELECT b.position, b.recognition_type, b.selection_reason,
       b.question_asset, b.answer_asset, q.*
FROM benchmark_items b JOIN questions q ON q.id = b.question_id
WHERE b.benchmark_id = 'ocr50-v1';

-- 공통 평가 문항을 제외한 목록. 실제 학습/평가 실행은 이 작업에 포함하지 않는다.
CREATE VIEW training_questions AS
SELECT q.* FROM questions q
WHERE NOT EXISTS (SELECT 1 FROM benchmark_items b WHERE b.question_id = q.id);
