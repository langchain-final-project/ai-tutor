"""SSE 전송 순서·재연결·실시간 전송·연결 종료를 검증한다."""

import asyncio
from contextlib import closing
from io import BytesIO
import json
from pathlib import Path
import sqlite3
from threading import Event

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from app import ocr_api
from app.db.submissions import submission_connection
from app.submission_api import create_submission_app


HEADERS = {"Authorization": "Bearer stream-test-token"}
MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "stream.sqlite3"
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


class FixturePipeline:
    def __init__(self, count=3, mode="needs_review", entered=None, release=None):
        self.count = count
        self.mode = mode
        self.entered = entered
        self.release = release

    def stream(self, image):
        assert image
        yield {"type": "stage", "stage": "qwen"}
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            assert self.release.wait(10), "테스트용 OCR 대기 시간 초과"
        if self.mode == "exception":
            raise RuntimeError("fixture unavailable")
        if self.mode == "missing_result":
            return
        text = ""
        for index in range(self.count):
            chunk = f"문장 {index}\n"
            text += chunk
            yield {"type": "delta", "text": chunk}
        yield {
            "type": "result",
            "result": {
                "status": self.mode,
                "text": text if self.mode != "failed" else "",
                "issues": [],
                "regions": [],
            },
        }


def make_app(database, factory=FixturePipeline, owner="student_A"):
    return create_submission_app(
        database,
        dev_token="stream-test-token",
        owner_id=owner,
        pipeline_factory=factory,
    )


def start_job(client):
    output = BytesIO()
    Image.new("RGB", (8, 8), "white").save(output, format="PNG")
    uploaded = client.post(
        "/submissions/photo",
        headers=HEADERS,
        files={"files": ("test.png", output.getvalue(), "image/png")},
    )
    assert uploaded.status_code == 201, uploaded.text
    submission_id = uploaded.json()["id"]
    response = client.post(
        f"/submissions/{submission_id}/ocr", headers=HEADERS
    )
    assert response.status_code == 202, response.text
    return submission_id, response.json()["id"]


def drain(app):
    app.state.ocr_jobs.executor.submit(lambda: None).result(timeout=10)


def parse_events(text):
    events = []
    for frame in text.split("\n\n"):
        fields = {}
        for line in frame.split("\n"):
            if line and not line.startswith(":"):
                name, value = line.split(":", 1)
                fields[name] = value.lstrip(" ")
        if "event" in fields:
            fields["data"] = json.loads(fields["data"])
            if "id" in fields:
                fields["id"] = int(fields["id"])
            events.append(fields)
    return events


def test_stream_replays_all_batches_and_preserves_unicode(database):
    app = make_app(database, lambda: FixturePipeline(count=205))
    with TestClient(app) as client:
        submission_id, job_id = start_job(client)
        drain(app)
        response = client.get(f"/ocr-jobs/{job_id}/events", headers=HEADERS)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["x-accel-buffering"] == "no"
        events = parse_events(response.text)
        assert len(events) > 200
        ids = [event["id"] for event in events]
        assert ids == sorted(set(ids))
        assert [event["event"] for event in events[:3]] == [
            "queued", "running", "stage"
        ]
        assert events[-1]["event"] == "completed"
        deltas = [event["data"]["text"] for event in events if event["event"] == "delta"]
        assert deltas == [f"문장 {index}\n" for index in range(205)]
        assert events[-1]["data"]["input_version_id"] is not None
        current = client.get(
            f"/submissions/{submission_id}", headers=HEADERS
        ).json()
        assert current["latest_input"]["content_text"] == "".join(deltas)
        assert current["review_status"] == "needs_review"

        docs = app.openapi()["paths"]["/ocr-jobs/{job_id}/events"]["get"]
        assert set(docs["responses"]["200"]["content"]) == {"text/event-stream"}


def test_stream_resume_with_query_or_last_event_header(database):
    app = make_app(database)
    with TestClient(app) as client:
        _, job_id = start_job(client)
        drain(app)
        url = f"/ocr-jobs/{job_id}/events"
        all_events = parse_events(client.get(url, headers=HEADERS).text)
        cursor = all_events[2]["id"]
        expected = [event for event in all_events if event["id"] > cursor]
        assert parse_events(client.get(
            url, headers=HEADERS, params={"after_id": cursor}
        ).text) == expected
        # 재연결 헤더가 최초 연결에 사용한 쿼리보다 우선한다.
        assert parse_events(client.get(
            url,
            headers={**HEADERS, "Last-Event-ID": str(cursor)},
            params={"after_id": all_events[-1]["id"]},
        ).text) == expected
        finished = client.get(
            url,
            headers={**HEADERS, "Last-Event-ID": str(all_events[-1]["id"])},
        )
        assert finished.status_code == 200
        assert parse_events(finished.text) == []
        with submission_connection(database) as connection:
            assert connection.execute("SELECT COUNT(*) FROM problem_ocr_jobs").fetchone()[0] == 1


def test_stream_rejects_invalid_cursors_before_sending_headers(database):
    app = make_app(database)
    with TestClient(app) as client:
        _, job_id = start_job(client)
        drain(app)
        url = f"/ocr-jobs/{job_id}/events"
        for value in ("-1", "not-a-number", "1.5", str(2**63)):
            response = client.get(
                url, headers={**HEADERS, "Last-Event-ID": value}
            )
            assert response.status_code == 400
            assert response.headers["content-type"].startswith("application/json")
        for value in (-1, 2**63):
            assert client.get(
                url, headers=HEADERS, params={"after_id": value}
            ).status_code == 422


def test_stream_authentication_and_owner_isolation(database):
    app = make_app(database)
    with TestClient(app) as client:
        _, job_id = start_job(client)
        drain(app)
        url = f"/ocr-jobs/{job_id}/events"
        assert client.get(url).status_code == 401
        assert client.get(
            url, headers={"Authorization": "Bearer wrong-token"}
        ).status_code == 401
        assert client.get(
            "/ocr-jobs/missing/events", headers=HEADERS
        ).status_code == 404
    with TestClient(make_app(database, owner="student_B")) as other:
        response = other.get(url, headers=HEADERS)
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/json")


@pytest.mark.parametrize("mode", ["failed", "incomplete", "exception", "missing_result"])
def test_failed_job_sends_terminal_failure_and_closes(database, mode):
    app = make_app(database, lambda: FixturePipeline(mode=mode))
    with TestClient(app) as client:
        submission_id, job_id = start_job(client)
        drain(app)
        response = client.get(f"/ocr-jobs/{job_id}/events", headers=HEADERS)
        assert response.status_code == 200
        events = parse_events(response.text)
        assert events[-1]["event"] == "failed"
        assert events[-1]["data"]["error_message"]
        assert all(event["event"] != "completed" for event in events)
        assert client.get(
            f"/submissions/{submission_id}", headers=HEADERS
        ).json()["latest_input"] is None


def asgi_scope(job_id, spec_version="2.4"):
    path = f"/ocr-jobs/{job_id}/events"
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": spec_version},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"authorization", b"Bearer stream-test-token")],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
    }


def test_stage_is_sent_before_model_completion(database):
    entered, release = Event(), Event()
    app = make_app(
        database, lambda: FixturePipeline(entered=entered, release=release)
    )
    with TestClient(app) as client:
        try:
            submission_id, job_id = start_job(client)
            assert entered.wait(5)
            bodies = []

            async def scenario():
                incoming = asyncio.Queue()
                incoming.put_nowait({"type": "http.request", "body": b"", "more_body": False})

                async def send(message):
                    if message["type"] != "http.response.body":
                        return
                    body = message.get("body", b"")
                    bodies.append(body)
                    if b"event: stage\n" in body:
                        assert not release.is_set()
                        assert app.state.ocr_jobs.get_job(job_id, "student_A")["status"] == "running"
                        release.set()
                    if b"event: completed\n" in body:
                        assert app.state.ocr_jobs.get_job(job_id, "student_A")["status"] == "completed"

                await asyncio.wait_for(
                    app(asgi_scope(job_id), incoming.get, send), timeout=5
                )

            asyncio.run(scenario())
            assert release.is_set()
            events = parse_events(b"".join(bodies).decode())
            assert events[-1]["event"] == "completed"
            assert len(bodies) > 3
            assert client.get(
                f"/submissions/{submission_id}", headers=HEADERS
            ).json()["review_status"] == "needs_review"
        finally:
            release.set()


def test_disconnection_keeps_job_running_and_reconnect_replays(database):
    entered, release = Event(), Event()
    app = make_app(
        database, lambda: FixturePipeline(entered=entered, release=release)
    )
    with TestClient(app) as client:
        try:
            _, job_id = start_job(client)
            assert entered.wait(5)
            bodies = []

            async def scenario():
                incoming = asyncio.Queue()
                incoming.put_nowait({"type": "http.request", "body": b"", "more_body": False})

                async def send(message):
                    if message["type"] == "http.response.body":
                        body = message.get("body", b"")
                        bodies.append(body)
                        if b"event: stage\n" in body:
                            incoming.put_nowait({"type": "http.disconnect"})

                await asyncio.wait_for(
                    app(asgi_scope(job_id, "2.3"), incoming.get, send), timeout=5
                )

            asyncio.run(scenario())
            assert not release.is_set()
            assert app.state.ocr_jobs.get_job(job_id, "student_A")["status"] == "running"
            before = parse_events(b"".join(bodies).decode())
            cursor = before[-1]["id"]
            release.set()
            drain(app)
            replayed = parse_events(client.get(
                f"/ocr-jobs/{job_id}/events",
                headers={**HEADERS, "Last-Event-ID": str(cursor)},
            ).text)
            assert replayed[-1]["event"] == "completed"
            assert all(event["id"] > cursor for event in replayed)
            assert all(event["event"] != "stage" for event in replayed)
        finally:
            release.set()


def test_idle_stream_has_heartbeat_without_event_id(monkeypatch):
    monkeypatch.setattr(ocr_api, "SSE_HEARTBEAT_SECONDS", 0)
    monkeypatch.setattr(ocr_api, "SSE_POLL_SECONDS", 0)

    class Request:
        calls = 0

        async def is_disconnected(self):
            self.calls += 1
            return self.calls > 1

    class Manager:
        def read_events(self, *_args):
            return {"status": "running"}, []

    async def scenario():
        return [frame async for frame in ocr_api._stream_events(
            Request(), Manager(), "job", "owner", 0
        )]

    frames = asyncio.run(scenario())
    assert frames == ["retry: 1000\n\n", ": keep-alive\n\n"]


def test_stream_read_error_preserves_cursor_and_hides_internal_details():
    class Request:
        async def is_disconnected(self):
            return False

    class Manager:
        def read_events(self, *_args):
            raise sqlite3.OperationalError("private database path")

    async def scenario():
        return [frame async for frame in ocr_api._stream_events(
            Request(), Manager(), "job", "owner", 5
        )]

    frames = asyncio.run(scenario())
    events = parse_events("".join(frames))
    assert events[0]["event"] == "stream_error"
    assert "id" not in events[0]
    assert "private database path" not in "".join(frames)
