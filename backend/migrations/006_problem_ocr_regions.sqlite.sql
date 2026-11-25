-- OCR 작업마다 사용한 원본 사진 영역을 고정해 저장한다.
ALTER TABLE problem_ocr_jobs ADD COLUMN selected_region_json TEXT
    CHECK (
        selected_region_json IS NULL
        OR (json_valid(selected_region_json)
            AND json_type(selected_region_json) = 'object')
    );

CREATE TRIGGER trg_ocr_job_region_immutable
BEFORE UPDATE OF selected_region_json ON problem_ocr_jobs
WHEN NEW.selected_region_json IS NOT OLD.selected_region_json
BEGIN
    SELECT RAISE(ABORT, 'OCR job input region is immutable');
END;
