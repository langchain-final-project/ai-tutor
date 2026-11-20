"""실제 모델 대신 테스트용 OCR로 작업 API와 수명 주기를 검증한다."""

from contextlib import closing
from io import BytesIO
from pathlib import Path
import sqlite3
from threading import Event
from uuid import uuid4

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from app.db.submissions import create_photo_submission, submission_connection
from app.submission_api import create_submission_app


HEADERS = {"Authorization": "Bearer test-token"}
MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


@pytest.fixture
def database(tmp_path: Path) -> Path:
    path = tmp_path / "ocr-api.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        for name in (
            "001_problem_bank.sqlite.sql",
            "002_problem_classification.sqlite.sql",
            "004_problem_submissions.sqlite.sql",
            "005_problem_ocr_jobs.sqlite.sql",
        ):
            connection.executescript(
                (MIGRATIONS / name).read_text(encoding="utf-8")
            )
    return path


def image_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (8, 8), "white").save(output, format="PNG")
    return output.getvalue()


class FixturePipeline:
    def __init__(self, mode="needs_review", entered=None, release=None):
        self.mode = mode
        self.entered = entered
        self.release = release

    def stream(self, image):
        assert image == image_bytes()
        yield {"type": "stage", "stage": "qwen"}
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            assert self.release.wait(10), "테스트용 OCR 대기 시간 초과"
        if self.mode == "exception":
            raise RuntimeError("fixture: model server unavailable")
        if self.mode == "missing_result":
            return
        yield {"type": "delta", "text": "3x = 9"}
        yield {
            "type": "result",
            "result": {
                "status": self.mode,
                "text": "" if self.mode == "failed" else "3x = 9",
                "rotation_ccw": 0,
                "issues": [],
                "regions": [],
            },
        }


def make_app(database, factory=FixturePipeline, owner="student_A"):
    return create_submission_app(
        database,
        dev_token="test-token",
        owner_id=owner,
        pipeline_factory=factory,
    )


def upload(client) -> int:
    response = client.post(
        "/submissions/photo",
        headers=HEADERS,
        files={"files": ("math.png", image_bytes(), "image/png")},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def drain(app):
    # 같은 작업 스레드의 다음 작업을 기다려 앞선 OCR 완료를 확인한다.
    app.state.ocr_jobs.executor.submit(lambda: None).result(timeout=10)


def test_job_lifecycle_and_editable_result(database):
    app = make_app(database)
    assert app.state.ocr_jobs.executor is None
    with TestClient(app) as client:
        assert app.state.ocr_jobs.executor is not None
        submission_id = upload(client)
        response = client.post(
            f"/submissions/{submission_id}/ocr", headers=HEADERS
        )
        assert response.status_code == 202, response.text
        job_id = response.json()["id"]
        assert "owner_id" not in response.json()
        drain(app)

        job = client.get(f"/ocr-jobs/{job_id}", headers=HEADERS)
        assert job.status_code == 200
        assert job.json()["status"] == "completed"
        assert job.json()["ocr_result_id"] is not None
        assert job.json()["started_at"] is not None
        assert job.json()["finished_at"] is not None

        current = client.get(
            f"/submissions/{submission_id}", headers=HEADERS
        ).json()
        assert current["review_status"] == "needs_review"
        assert current["latest_input"]["version_number"] == 1
        assert current["latest_input"]["content_text"] == "3x = 9"
        assert current["latest_input"]["confirmed_at"] is None

        confirmed = client.post(
            f"/submissions/{submission_id}/confirm",
            headers=HEADERS,
            json={"content_text": "-3x = 9", "expected_version_number": 1},
        )
        assert confirmed.status_code == 200
        assert confirmed.json()["review_status"] == "confirmed"
        assert confirmed.json()["latest_input"]["version_number"] == 2
        assert client.post(
            f"/submissions/{submission_id}/ocr", headers=HEADERS
        ).status_code == 409

    assert app.state.ocr_jobs.executor is None


def test_selected_remote_model_is_recorded_in_ocr_history(database, monkeypatch):
    model_name = "qwen3-vl:4b-instruct-q4_K_M"
    monkeypatch.setenv("DIDGO_QWEN_MODEL", model_name)
    app = make_app(database)
    with TestClient(app) as client:
        submission_id = upload(client)
        assert client.post(f"/submissions/{submission_id}/ocr", headers=HEADERS).status_code == 202
        drain(app)
        history = client.get(f"/submissions/{submission_id}/ocr/history", headers=HEADERS).json()
        assert len(history) == 1
        assert history[0]["model_name"] == model_name


def test_running_queued_and_duplicate_requests(database):
    entered, release = Event(), Event()
    app = make_app(
        database, lambda: FixturePipeline(entered=entered, release=release)
    )
    with TestClient(app) as client:
        try:
            first, second = upload(client), upload(client)
            response = client.post(
                f"/submissions/{first}/ocr", headers=HEADERS
            )
            assert response.status_code == 202
            first_job = response.json()["id"]
            assert entered.wait(5)
            assert client.get(
                f"/ocr-jobs/{first_job}", headers=HEADERS
            ).json()["status"] == "running"

            duplicate = client.post(
                f"/submissions/{first}/ocr", headers=HEADERS
            )
            assert duplicate.status_code == 202
            assert duplicate.json()["id"] == first_job

            queued = client.post(
                f"/submissions/{second}/ocr", headers=HEADERS
            )
            assert queued.status_code == 202
            assert queued.json()["status"] == "queued"
        finally:
            release.set()
        drain(app)


@pytest.mark.parametrize(
    "mode", ["failed", "incomplete", "exception", "missing_result"]
)
def test_failed_ocr_is_recorded_and_can_be_retried(database, mode):
    app = make_app(database, lambda: FixturePipeline(mode))
    with TestClient(app) as client:
        submission_id = upload(client)
        response = client.post(
            f"/submissions/{submission_id}/ocr", headers=HEADERS
        )
        assert response.status_code == 202
        job_id = response.json()["id"]
        drain(app)

        job = client.get(f"/ocr-jobs/{job_id}", headers=HEADERS).json()
        assert job["status"] == "failed"
        assert job["error_message"]
        assert job["ocr_result_id"] is not None
        assert client.get(
            f"/submissions/{submission_id}", headers=HEADERS
        ).json()["latest_input"] is None
        history = client.get(
            f"/submissions/{submission_id}/ocr/history", headers=HEADERS
        ).json()
        assert len(history) == (1 if mode == "exception" else 2)
        assert all(item["status"] == "failed" for item in history)

        app.state.ocr_jobs.pipeline_factory = FixturePipeline
        retry = client.post(
            f"/submissions/{submission_id}/ocr", headers=HEADERS
        )
        assert retry.status_code == 202
        assert retry.json()["id"] != job_id
        drain(app)
        assert client.get(
            f"/ocr-jobs/{retry.json()['id']}", headers=HEADERS
        ).json()["status"] == "completed"


def test_job_authentication_and_owner_isolation(database):
    app = make_app(database)
    with TestClient(app) as client:
        submission_id = upload(client)
        assert client.post(
            f"/submissions/{submission_id}/ocr"
        ).status_code == 401
        assert client.post(
            "/submissions/9999/ocr", headers=HEADERS
        ).status_code == 404
        assert client.post(
            "/submissions/0/ocr", headers=HEADERS
        ).status_code == 422
        response = client.post(
            f"/submissions/{submission_id}/ocr", headers=HEADERS
        )
        assert response.status_code == 202
        job_id = response.json()["id"]
        drain(app)
        assert client.get(f"/ocr-jobs/{job_id}").status_code == 401

    with TestClient(make_app(database, owner="student_B")) as other:
        assert other.get(
            f"/ocr-jobs/{job_id}", headers=HEADERS
        ).status_code == 404
        assert other.post(
            f"/submissions/{submission_id}/ocr", headers=HEADERS
        ).status_code == 404
        assert other.get(
            "/ocr-jobs/missing", headers=HEADERS
        ).status_code == 404


def test_startup_recovers_queued_and_interrupted_jobs(database):
    with submission_connection(database) as connection:
        submissions = [
            create_photo_submission(
                connection,
                owner_id="student_A",
                original_filename="math.png",
                mime_type="image/png",
                image_content=image_bytes(),
            )["submission_id"]
            for _ in range(2)
        ]
        queued_id, running_id = str(uuid4()), str(uuid4())
        for job_id, submission_id in zip(
            (queued_id, running_id), submissions
        ):
            connection.execute(
                "INSERT INTO problem_ocr_jobs (id, submission_id) VALUES (?, ?)",
                (job_id, submission_id),
            )
        connection.execute(
            """
            UPDATE problem_ocr_jobs SET status = 'running',
                started_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE id = ?
            """,
            (running_id,),
        )

    app = make_app(database)
    with TestClient(app) as client:
        drain(app)
        assert client.get(
            f"/ocr-jobs/{queued_id}", headers=HEADERS
        ).json()["status"] == "completed"
        interrupted = client.get(
            f"/ocr-jobs/{running_id}", headers=HEADERS
        ).json()
        assert interrupted["status"] == "failed"
        assert "재시작" in interrupted["error_message"]

    assert app.state.ocr_jobs.executor is None
