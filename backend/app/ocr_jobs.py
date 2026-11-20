"""OCR 작업을 순서대로 실행하고 상태·스트리밍 이벤트를 기록한다."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from copy import copy
from dataclasses import replace
import json
import logging
import os
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4
from app.ocr.condition_extractor import ConditionExtractor

from app.db.submissions import (
    create_initial_input_version,
    get_submission,
    get_uploaded_photo,
    record_ocr_attempt,
    submission_connection,
)
from app.ocr import Backend, OCRPipeline
from app.ocr.llama import OCRError
from app.ocr.orientation import get_orientation_detector
from app.ocr.layout_detector import get_layout_detector
from app.ocr.formula import get_formula_backend
from app.ocr.selection import crop_photo_region, validate_photo_region
from app.ocr.retry import (
    MAX_ATTEMPTS, OCR_BUDGET_SECONDS, assess_result, critical_signature, select_result,
)

logger = logging.getLogger(__name__)


def configured_qwen_model():
    return os.getenv("DIDGO_QWEN_MODEL", "").strip() or None


def build_qwen_pipeline():
    qwen_url = os.getenv(
        "DIDGO_QWEN_URL",
        "http://127.0.0.1:8081/v1",
    )

    backend = Backend(
        url=qwen_url,
        timeout=float(os.getenv("DIDGO_OCR_TIMEOUT", "120")),
        image_tokens=(256, 2048),
        model_name=configured_qwen_model(),
    )

    condition_backend = Backend(
        url=qwen_url,
        timeout=float(
            os.getenv("DIDGO_CONDITION_TIMEOUT", "180")
        ),
        model_name=configured_qwen_model(),
    )

    return OCRPipeline(
        backend,
        formula=get_formula_backend(),
        primary_engine="qwen",
        orientation_detector=get_orientation_detector(),
        layout_detector=get_layout_detector(),
        condition_extractor=ConditionExtractor(condition_backend),
    )


class OCRJobManager:
    def __init__(
        self,
        database: str | Path,
        *,
        pipeline_factory=build_qwen_pipeline,
        model_name=None,
        model_version=None,
        pipeline_version="qwen-api-v7-region-assembly",
    ):
        self.database = Path(database)
        self.pipeline_factory = pipeline_factory
        self.model_name = model_name if model_name is not None else configured_qwen_model() or "Qwen3-VL-4B-Instruct"
        self.model_version = model_version
        self.pipeline_version = pipeline_version
        self.executor = None

    @contextmanager
    def connection(self):
        import sqlite3

        with submission_connection(self.database) as connection:
            connection.row_factory = sqlite3.Row
            yield connection

    def _job(self, connection, job_id, owner_id=None):
        sql = """
            SELECT j.*, s.owner_id
            FROM problem_ocr_jobs j
            JOIN problem_submissions s ON s.id = j.submission_id
            WHERE j.id = ?
        """
        parameters = [job_id]

        if owner_id is not None:
            sql += " AND s.owner_id = ?"
            parameters.append(owner_id)

        row = connection.execute(sql, parameters).fetchone()

        if row is None:
            raise LookupError(
                "OCR 작업이 없거나 접근 권한이 없습니다."
            )

        job = dict(row)
        raw_region = job.pop("selected_region_json", None)
        job["selected_region"] = json.loads(raw_region) if raw_region is not None else None
        return job

    def _event(self, connection, job_id, event_type, payload):
        connection.execute(
            """
            INSERT INTO problem_ocr_job_events (
                job_id, event_type, payload_json
            )
            VALUES (?, ?, ?)
            """,
            (
                job_id,
                event_type,
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    allow_nan=False,
                ),
            ),
        )

    def get_job(self, job_id, owner_id):
        with self.connection() as connection:
            return self._job(connection, job_id, owner_id)

    def read_events(self, job_id, owner_id, after_id=0):
        if type(after_id) is not int or after_id < 0:
            raise ValueError(
                "이벤트 번호는 0 이상의 정수여야 합니다."
            )

        with self.connection() as connection:
            job = self._job(connection, job_id, owner_id)

            rows = connection.execute(
                """
                SELECT *
                FROM problem_ocr_job_events
                WHERE job_id = ? AND id > ?
                ORDER BY id
                LIMIT 200
                """,
                (job_id, after_id),
            ).fetchall()

            events = []

            for row in rows:
                event = dict(row)
                event["payload"] = json.loads(
                    event.pop("payload_json")
                )
                events.append(event)

            return job, events

    def start(self):
        if self.executor is not None:
            return

        with self.connection() as connection:
            running = [
                row["id"]
                for row in connection.execute(
                    """
                    SELECT id FROM problem_ocr_jobs
                    WHERE status = 'running'
                    """
                )
            ]

            queued = [
                row["id"]
                for row in connection.execute(
                    """
                    SELECT id FROM problem_ocr_jobs
                    WHERE status = 'queued'
                    ORDER BY created_at, rowid
                    """
                )
            ]

        for job_id in running:
            self._mark_failed(
                job_id,
                "서버 재시작으로 OCR 작업이 중단되었습니다.",
            )

        self.executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="didgo-ocr",
        )

        for job_id in queued:
            self.executor.submit(self._run, job_id)

    def shutdown(self):
        if self.executor is not None:
            self.executor.shutdown(
                wait=True,
                cancel_futures=True,
            )
            self.executor = None

    def submit(self, *, submission_id, owner_id, selected_region=None):
        if self.executor is None:
            raise RuntimeError(
                "OCR 작업 관리자가 시작되지 않았습니다."
            )

        if selected_region is not None:
            with self.connection() as connection:
                photo = get_uploaded_photo(connection, submission_id=submission_id, owner_id=owner_id)
            # 큰 사진의 디코딩은 DB 트랜잭션 밖에서 수행한다.
            selected_region = validate_photo_region(photo["image_content"], selected_region)

        with self.connection() as connection:
            submission = get_submission(
                connection,
                submission_id=submission_id,
                owner_id=owner_id,
            )

            if submission["input_kind"] != "photo":
                raise ValueError(
                    "사진 문제에만 OCR을 실행할 수 있습니다."
                )

            if submission["latest_input"] is not None:
                raise ValueError(
                    "학생 수정 단계가 시작되어 OCR이 종료되었습니다."
                )

            active = connection.execute(
                """
                SELECT id FROM problem_ocr_jobs
                WHERE submission_id = ?
                  AND status IN ('queued', 'running')
                """,
                (submission_id,),
            ).fetchone()

            if active is not None:
                active_job = self._job(
                    connection,
                    active["id"],
                    owner_id,
                )
                if active_job["selected_region"] != selected_region:
                    raise ValueError("이미 다른 범위의 OCR이 진행 중입니다. 완료 후 확인해 주세요.")
                return active_job

            job_id = str(uuid4())

            if selected_region is None:
                connection.execute(
                    "INSERT INTO problem_ocr_jobs (id, submission_id) VALUES (?, ?)",
                    (job_id, submission_id),
                )
            else:
                connection.execute(
                    "INSERT INTO problem_ocr_jobs (id, submission_id, selected_region_json) VALUES (?, ?, ?)",
                    (job_id, submission_id, json.dumps(selected_region, allow_nan=False)),
                )

            self._event(
                connection,
                job_id,
                "queued",
                {"job_id": job_id, "status": "queued", "selected_region": selected_region},
            )

        try:
            self.executor.submit(self._run, job_id)

        except RuntimeError:
            self._mark_failed(
                job_id,
                "OCR 작업을 시작하지 못했습니다.",
            )
            raise

        return self.get_job(job_id, owner_id)

    def _begin(self, job_id):
        with self.connection() as connection:
            job = self._job(connection, job_id)

            if job["status"] != "queued":
                return None

            submission = get_submission(
                connection,
                submission_id=job["submission_id"],
                owner_id=job["owner_id"],
            )

            if submission["latest_input"] is not None:
                raise ValueError(
                    "이미 학생 수정 단계로 넘어간 문제입니다."
                )

            photo = get_uploaded_photo(
                connection,
                submission_id=job["submission_id"],
                owner_id=job["owner_id"],
            )

            connection.execute(
                """
                UPDATE problem_ocr_jobs
                SET status = 'running',
                    started_at =
                        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ?
                """,
                (job_id,),
            )

            self._event(
                connection,
                job_id,
                "running",
                {"job_id": job_id, "status": "running"},
            )

            return photo["image_content"], job["selected_region"]

    def _publish(self, job_id, events):
        if not events:
            return

        with self.connection() as connection:
            if self._job(connection, job_id)["status"] != "running":
                raise ValueError(
                    "실행 중인 OCR 작업이 아닙니다."
                )

            for event in events:
                self._event(
                    connection,
                    job_id,
                    event["type"],
                    event,
                )

    def _record_attempt(self, job_id, result):
        with self.connection() as connection:
            job = self._job(connection, job_id)
            return record_ocr_attempt(
                connection, submission_id=job["submission_id"], owner_id=job["owner_id"],
                model_name=self.model_name, model_version=self.model_version,
                pipeline_version=self.pipeline_version,
                result={**result, "selected_region": job["selected_region"]},
            )

    def _finish(self, job_id, result, ocr_id=None):
        with self.connection() as connection:
            job = self._job(connection, job_id)

            if job["status"] not in {"queued", "running"}:
                return

            result = {**result, "selected_region": job["selected_region"]}
            if ocr_id is None:
                ocr_id = record_ocr_attempt(
                    connection, submission_id=job["submission_id"], owner_id=job["owner_id"],
                    model_name=self.model_name, model_version=self.model_version,
                    pipeline_version=self.pipeline_version, result=result,
                )
            else:
                # 저장한 전사 원문은 보존하고 최종 선택·검토 정보만 함께 기록한다.
                connection.execute(
                    "UPDATE problem_ocr_results SET raw_response_json = ? WHERE id = ?",
                    (json.dumps(result, ensure_ascii=False, allow_nan=False), ocr_id),
                )

            succeeded = result["status"] == "needs_review"
            version_id = None

            if succeeded:
                version_id = create_initial_input_version(
                    connection,
                    submission_id=job["submission_id"],
                    owner_id=job["owner_id"],
                    ocr_result_id=ocr_id,
                )

            status = "completed" if succeeded else "failed"

            error_message = None if succeeded else (
                "인식 서버의 연결·설정을 확인한 뒤 다시 시도해 주세요."
                if result.get("retryable_error") is False or any(
                    region.get("retryable_error") is False for region in result.get("regions", [])
                )
                else "문제를 끝까지 읽지 못했어요. 사진을 다시 촬영하거나 다른 사진을 올려주세요."
            )

            connection.execute(
                """
                UPDATE problem_ocr_jobs
                SET status = ?,
                    ocr_result_id = ?,
                    error_message = ?,
                    finished_at =
                        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ?
                """,
                (status, ocr_id, error_message, job_id),
            )

            self._event(
                connection,
                job_id,
                status,
                {
                    "job_id": job_id,
                    "submission_id": job["submission_id"],
                    "status": status,
                    "ocr_result_id": ocr_id,
                    "input_version_id": version_id,
                    "error_message": error_message,
                },
            )

    def _mark_failed(self, job_id, message):
        with self.connection() as connection:
            job = self._job(connection, job_id)

            if job["status"] not in {"queued", "running"}:
                return

            connection.execute(
                """
                UPDATE problem_ocr_jobs
                SET status = 'failed',
                    error_message = ?,
                    finished_at =
                        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ?
                """,
                (message, job_id),
            )

            self._event(
                connection,
                job_id,
                "failed",
                {
                    "job_id": job_id,
                    "status": "failed",
                    "error_message": message,
                },
            )

    def _read_attempt(self, job_id, image, pipeline, attempt):
        pending = []
        last_flush = monotonic()
        result = None
        try:
            with closing(pipeline.stream(image)) as stream:
                for event in stream:
                    if event["type"] == "result":
                        result = event["result"]
                        break
                    pending.append({**event, "attempt": attempt})
                    if event["type"] != "delta" or len(pending) >= 20 or monotonic() - last_flush >= 0.1:
                        self._publish(job_id, pending)
                        pending = []
                        last_flush = monotonic()
        except Exception as error:
            logger.exception("OCR 시도 실패: %s / %s", job_id, attempt)
            result = {
                "status": "failed", "text": "", "issues": [str(error)],
                "error_kind": "transport" if isinstance(error, OCRError) else "configuration",
                "retryable_error": isinstance(error, OCRError) and getattr(error, "retryable", True),
            }
        self._publish(job_id, pending)
        return result if result is not None else {
            "status": "failed", "text": "", "issues": ["OCR 최종 결과를 받지 못했습니다."],
            "error_kind": "missing_result",
        }

    def _run(self, job_id):
        try:
            job_input = self._begin(job_id)

            if job_input is None:
                return

            image, selected_region = job_input
            if selected_region is not None:
                # 먼저 원본 좌표로 정확히 자른 뒤, 선택된 문제에 방향 보정·OCR을 적용한다.
                image = crop_photo_region(image, selected_region)

            results, result_ids = [], []
            deadline = monotonic() + OCR_BUDGET_SECONDS
            condition_pipeline = None
            skipped = None
            for number in range(1, MAX_ATTEMPTS + 1):
                remaining = deadline - monotonic()
                if number > 1 and remaining <= 1:
                    skipped = "time_budget"
                    break
                pipeline = copy(self.pipeline_factory())
                reasons = results[-1]["assessment"]["retry_reasons"] if results else []
                variant = "original"
                tokens = 2048 if number > 1 and "output_truncated" in reasons else 1024
                if isinstance(pipeline, OCRPipeline):
                    if condition_pipeline is None:
                        condition_pipeline = copy(pipeline)
                    # 부가 조건은 선택된 본문에 대해서만 한 번 추출한다.
                    pipeline.condition_extractor = None
                    pipeline.max_tokens = tokens
                    pipeline.deadline = deadline
                    pipeline.retry_preprocess = number > 1 and not (
                        "output_truncated" in reasons or "transport_error" in reasons
                    )
                    variant = "contrast_1.3" if pipeline.retry_preprocess else "original"
                    if isinstance(pipeline.paddle, Backend):
                        pipeline.paddle = replace(
                            pipeline.paddle, timeout=max(1, min(pipeline.paddle.timeout, 120, remaining))
                        )
                if number > 1:
                    self._publish(job_id, [{"type": "stage", "stage": "retry", "attempt": number, "reasons": reasons}])
                    if "transport_error" in reasons:
                        sleep(1)
                started = monotonic()
                result = self._read_attempt(job_id, image, pipeline, number)
                assessment = assess_result(result)
                if not assessment["usable"]:
                    result = {**result, "raw_text": result.get("text", ""), "text": "", "status": "failed"}
                result.update({
                    "attempt_number": number, "assessment": assessment,
                    "input_variant": variant, "max_tokens": tokens,
                    "seconds": round(monotonic() - started, 3),
                })
                result_id = self._record_attempt(job_id, result)
                results.append(result)
                result_ids.append(result_id)
                self._publish(job_id, [{
                    "type": "attempt_complete", "attempt": number, "ocr_result_id": result_id,
                    "retry_reasons": assessment["retry_reasons"],
                }])
                if not assessment["retry_reasons"]:
                    break

            verified = None
            verification = "not_needed"
            if len(results) == 2 and all(assess_result(item)["usable"] for item in results) and (
                critical_signature(results[0]["text"]) != critical_signature(results[1]["text"])
            ):
                verification = "uncertain"
                if condition_pipeline is not None and condition_pipeline.primary_engine == "qwen" and deadline - monotonic() > 1:
                    verifier = copy(condition_pipeline)
                    if isinstance(verifier.paddle, Backend):
                        verifier.paddle = replace(verifier.paddle, timeout=min(15, deadline - monotonic()))
                    self._publish(job_id, [{"type": "stage", "stage": "verification"}])
                    try:
                        verified = verifier.verify_candidates(image, results)
                        if verified is not None:
                            verification = "checked"
                    except Exception:
                        logger.exception("원본 대조를 완료하지 못했습니다: %s", job_id)
                        verification = "unavailable"
            selected, summary = select_result(results, verified)
            summary.update({
                "verification": verification, "retry_skipped": skipped,
                "attempts": [{
                    "attempt": index + 1, "ocr_result_id": result_ids[index],
                    "retry_reasons": item["assessment"]["retry_reasons"],
                    "input_variant": item["input_variant"], "max_tokens": item["max_tokens"],
                    "seconds": item["seconds"],
                } for index, item in enumerate(results)],
            })
            result = {**results[selected], "retry_summary": summary, "issues": list(results[selected].get("issues", []))}
            result["issues"].extend(warning["message"] for warning in summary["review_warnings"])
            if result["status"] == "needs_review" and condition_pipeline is not None and condition_pipeline.condition_extractor is not None:
                with closing(condition_pipeline.complete_conditions(image, result)) as events:
                    for event in events:
                        self._publish(job_id, [event])
            self._finish(job_id, result, result_ids[selected])

        except Exception as error:
            logger.exception("OCR 작업 실패: %s", job_id)

            try:
                self._finish(
                    job_id,
                    {
                        "status": "failed",
                        "text": "",
                        "issues": [str(error)],
                    },
                )

            except Exception:
                logger.exception(
                    "OCR 실패 결과 저장 실패: %s",
                    job_id,
                )

                self._mark_failed(
                    job_id,
                    "OCR 결과를 저장하지 못했습니다.",
                )
