"""OCR 작업을 순서대로 실행하고 상태·스트리밍 이벤트를 기록한다."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import json
import logging
import os
from pathlib import Path
from time import monotonic
from uuid import uuid4

from app.db.submissions import (
    create_initial_input_version,
    get_submission,
    get_uploaded_photo,
    record_ocr_attempt,
    submission_connection,
)
from app.ocr import Backend, OCRPipeline

logger = logging.getLogger(__name__)


def build_qwen_pipeline():
    backend = Backend(
        url=os.getenv(
            "DIDGO_QWEN_URL",
            "http://127.0.0.1:8081/v1",
        ),
        timeout=float(os.getenv("DIDGO_OCR_TIMEOUT", "120")),
        image_tokens=(256, 2048),
    )
    return OCRPipeline(backend, primary_engine="qwen")


class OCRJobManager:
    def __init__(
        self,
        database: str | Path,
        *,
        pipeline_factory=build_qwen_pipeline,
        model_name="Qwen3-VL-2B",
        model_version=None,
        pipeline_version="qwen-api-v1",
    ):
        self.database = Path(database)
        self.pipeline_factory = pipeline_factory
        self.model_name = model_name
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

        return dict(row)

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

    def submit(self, *, submission_id, owner_id):
        if self.executor is None:
            raise RuntimeError(
                "OCR 작업 관리자가 시작되지 않았습니다."
            )

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
                return self._job(
                    connection,
                    active["id"],
                    owner_id,
                )

            job_id = str(uuid4())

            connection.execute(
                """
                INSERT INTO problem_ocr_jobs (
                    id, submission_id
                )
                VALUES (?, ?)
                """,
                (job_id, submission_id),
            )

            self._event(
                connection,
                job_id,
                "queued",
                {"job_id": job_id, "status": "queued"},
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

            return photo["image_content"]

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

    def _finish(self, job_id, result):
        with self.connection() as connection:
            job = self._job(connection, job_id)

            if job["status"] not in {"queued", "running"}:
                return

            ocr_id = record_ocr_attempt(
                connection,
                submission_id=job["submission_id"],
                owner_id=job["owner_id"],
                model_name=self.model_name,
                model_version=self.model_version,
                pipeline_version=self.pipeline_version,
                result=result,
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
                "일부 영역을 인식하지 못했습니다."
                if result["status"] == "incomplete"
                else "OCR 인식에 실패했습니다. OCR 이력을 확인해 주세요."
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

    def _run(self, job_id):
        try:
            image = self._begin(job_id)

            if image is None:
                return

            result = None
            pending = []
            last_flush = monotonic()

            pipeline = self.pipeline_factory()

            with closing(pipeline.stream(image)) as stream:
                for event in stream:
                    if event["type"] == "result":
                        result = event["result"]
                        break

                    pending.append(event)

                    if (
                        event["type"] != "delta"
                        or len(pending) >= 20
                        or monotonic() - last_flush >= 0.1
                    ):
                        self._publish(job_id, pending)
                        pending = []
                        last_flush = monotonic()

            self._publish(job_id, pending)

            if result is None:
                raise ValueError(
                    "OCR 최종 결과를 받지 못했습니다."
                )

            self._finish(job_id, result)

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