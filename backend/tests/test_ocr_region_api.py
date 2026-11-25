"""선택 영역·모델 입력·저장 결과가 같은 사진 범위를 가리키는지 검증한다."""

from contextlib import closing
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import sqlite3
from threading import Event
from uuid import uuid4

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from app.db.ocr_regions import ensure_ocr_region_schema
from app.db.submissions import create_photo_submission, submission_connection
from app.ocr.images import open_image
from app.submission_api import create_submission_app

HEADERS = {"Authorization": "Bearer test-token"}
MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "region.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        for name in (
            "001_problem_bank.sqlite.sql", "002_problem_classification.sqlite.sql",
            "004_problem_submissions.sqlite.sql", "005_problem_ocr_jobs.sqlite.sql",
            "006_problem_ocr_regions.sqlite.sql",
        ):
            connection.executescript((MIGRATIONS / name).read_text(encoding="utf-8"))
    return path


def photo_bytes(exif=False):
    with Image.new("RGB", (12, 8)) as image, BytesIO() as output:
        for y in range(image.height):
            for x in range(image.width):
                image.putpixel((x, y), (x * 17, y * 23, (x + y) * 9))
        if exif:
            orientation = Image.Exif()
            orientation[274] = 6
            image.save(output, format="JPEG", exif=orientation)
        else:
            image.save(output, format="PNG")
        return output.getvalue()


def region_for(content):
    with open_image(content) as image:
        return {
            "coordinate_space": "exif_transposed", "image_size": list(image.size),
            "bbox": [1, 2, image.width - 1, image.height - 1],
        }


class CapturePipeline:
    def __init__(self, received, entered=None, release=None):
        self.received, self.entered, self.release = received, entered, release

    def stream(self, content):
        self.received.append(content)
        if self.entered:
            self.entered.set()
        if self.release:
            assert self.release.wait(10)
        yield {"type": "stage", "stage": "fixture"}
        yield {"type": "result", "result": {
            "status": "needs_review", "text": "선택한 문제: x = 3", "rotation_ccw": 90,
            "orientation": {"status": "corrected", "rotation_ccw": 90},
            "selected_region": {"untrusted_model_value": True},
            "issues": [], "regions": [],
        }}


def make_app(database, received, owner="A", entered=None, release=None):
    return create_submission_app(
        database, dev_token="test-token", owner_id=owner,
        pipeline_factory=lambda: CapturePipeline(received, entered, release),
    )


def upload(client, content):
    response = client.post("/submissions/photo", headers=HEADERS,
                           files={"files": ("two-questions.jpg", content, "application/octet-stream")})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def drain(app):
    app.state.ocr_jobs.executor.submit(lambda: None).result(timeout=10)


@pytest.mark.parametrize("exif", [False, True])
def test_exact_crop_survives_orientation_edit_confirm_and_reconnect(database, exif):
    content = photo_bytes(exif)
    region = region_for(content)
    received = []
    app = make_app(database, received)
    with TestClient(app) as client:
        sid = upload(client, content)
        response = client.post(f"/submissions/{sid}/ocr", headers=HEADERS, json={"region": region})
        assert response.status_code == 202, response.text
        assert response.json()["selected_region"] == region
        job_id = response.json()["id"]
        drain(app)
        assert client.get(f"/ocr-jobs/{job_id}", headers=HEADERS).json()["status"] == "completed"

        with open_image(content) as original, original.crop(tuple(region["bbox"])) as expected:
            with open_image(received[0]) as actual:
                assert actual.size == expected.size
                assert actual.tobytes() == expected.tobytes()
            selected = client.get(f"/submissions/{sid}/photo?selected=true", headers=HEADERS)
            with Image.open(BytesIO(selected.content)) as displayed:
                assert displayed.tobytes() == expected.tobytes()
            selected = client.get(f"/submissions/{sid}/photo?selected=true&corrected=true", headers=HEADERS)
            with Image.open(BytesIO(selected.content)) as displayed, expected.rotate(90, expand=True) as rotated:
                assert displayed.size == rotated.size
                assert displayed.tobytes() == rotated.tobytes()

        assert client.get(f"/submissions/{sid}/photo", headers=HEADERS).content == content
        current = client.get(f"/submissions/{sid}", headers=HEADERS).json()
        assert current["selected_region"] == region
        assert current["latest_input"]["version_number"] == 1
        saved = client.patch(f"/submissions/{sid}/input", headers=HEADERS,
                             json={"content_text": "학생 수정: x = 4", "expected_version_number": 1})
        assert saved.status_code == 200
        assert saved.json()["selected_region"] == region
        confirmed = client.post(f"/submissions/{sid}/confirm", headers=HEADERS,
                                json={"content_text": "학생 수정: x = 4", "expected_version_number": 2})
        assert confirmed.status_code == 200
        assert confirmed.json()["selected_region"] == region
        assert confirmed.json()["review_status"] == "confirmed"
        history = client.get(f"/submissions/{sid}/ocr/history", headers=HEADERS).json()
        assert history[0]["raw_response"]["selected_region"] == region
        assert history[0]["ocr_text"] == "선택한 문제: x = 3"
        assert client.post(f"/submissions/{sid}/ocr", headers=HEADERS, json={"region": region}).status_code == 409

    with TestClient(make_app(database, received)) as fresh:
        restored = fresh.get(f"/submissions/{sid}", headers=HEADERS).json()
        assert restored["selected_region"] == region
        assert restored["latest_input"]["content_text"] == "학생 수정: x = 4"
        assert restored["review_status"] == "confirmed"
        assert len(received) == 1
    with TestClient(make_app(database, [], owner="B")) as other:
        assert other.get(f"/submissions/{sid}/photo?selected=true&corrected=true", headers=HEADERS).status_code == 404
        assert other.post(f"/submissions/{sid}/ocr", headers=HEADERS, json={"region": region}).status_code == 404


@pytest.mark.parametrize("change", [
    {"bbox": [0, 0, 13, 8]}, {"bbox": [2, 2, 2, 4]},
    {"bbox": [-1, 0, 4, 4]}, {"bbox": [0, 0, 4.5, 4]},
    {"bbox": [0, 0, True, 4]}, {"bbox": [0, 0, 4]},
    {"image_size": [8, 12]}, {"coordinate_space": "rotated"}, {"extra": "ignored?"},
])
def test_invalid_region_rejected_before_job_creation(database, change):
    received = []
    app = make_app(database, received)
    with TestClient(app) as client:
        content = photo_bytes()
        sid = upload(client, content)
        region = {**region_for(content), **change}
        response = client.post(f"/submissions/{sid}/ocr", headers=HEADERS, json={"region": region})
        assert response.status_code == 422, response.text
        assert not received
        with closing(sqlite3.connect(database)) as connection:
            assert connection.execute("SELECT COUNT(*) FROM problem_ocr_jobs").fetchone()[0] == 0


def test_duplicate_request_keeps_region_and_rejects_changed_region(database):
    received, entered, release = [], Event(), Event()
    app = make_app(database, received, entered=entered, release=release)
    with TestClient(app) as client:
        content = photo_bytes()
        sid = upload(client, content)
        region = region_for(content)
        try:
            first = client.post(f"/submissions/{sid}/ocr", headers=HEADERS, json={"region": region})
            assert entered.wait(5)
            repeated = client.post(f"/submissions/{sid}/ocr", headers=HEADERS, json={"region": region})
            assert repeated.status_code == 202
            assert repeated.json()["id"] == first.json()["id"]
            changed = deepcopy(region)
            changed["bbox"][0] = 0
            assert client.post(f"/submissions/{sid}/ocr", headers=HEADERS, json={"region": changed}).status_code == 409
            with closing(sqlite3.connect(database)) as connection:
                assert json.loads(connection.execute("SELECT selected_region_json FROM problem_ocr_jobs").fetchone()[0]) == region
                with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                    connection.execute("UPDATE problem_ocr_jobs SET selected_region_json = NULL")
        finally:
            release.set()
        drain(app)
        assert len(received) == 1


def test_queued_job_recovers_the_persisted_crop_after_restart(database):
    content = photo_bytes(True)
    region = region_for(content)
    job_id = str(uuid4())
    with submission_connection(database) as connection:
        sid = create_photo_submission(connection, owner_id="A", original_filename="math.jpg",
                                      mime_type="image/jpeg", image_content=content)["submission_id"]
        connection.execute("INSERT INTO problem_ocr_jobs(id, submission_id, selected_region_json) VALUES (?, ?, ?)",
                           (job_id, sid, json.dumps(region)))
    received = []
    app = make_app(database, received)
    with TestClient(app) as client:
        drain(app)
        job = client.get(f"/ocr-jobs/{job_id}", headers=HEADERS).json()
        assert job["status"] == "completed"
        assert job["selected_region"] == region
        with open_image(content) as source, source.crop(tuple(region["bbox"])) as expected, open_image(received[0]) as actual:
            assert actual.tobytes() == expected.tobytes()


def test_region_migration_is_repeatable_and_keeps_existing_jobs(tmp_path):
    database = tmp_path / "old.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        for name in ("001_problem_bank.sqlite.sql", "002_problem_classification.sqlite.sql",
                     "004_problem_submissions.sqlite.sql", "005_problem_ocr_jobs.sqlite.sql"):
            connection.executescript((MIGRATIONS / name).read_text(encoding="utf-8"))
        connection.execute("PRAGMA foreign_keys = ON")
        sid = create_photo_submission(connection, owner_id="A", original_filename="math.png",
                                      mime_type="image/png", image_content=photo_bytes())["submission_id"]
        connection.execute("INSERT INTO problem_ocr_jobs(id, submission_id) VALUES ('old-job', ?)", (sid,))
        connection.commit()
        ensure_ocr_region_schema(connection)
        ensure_ocr_region_schema(connection)
        assert connection.execute("SELECT id, selected_region_json FROM problem_ocr_jobs").fetchone() == ("old-job", None)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
