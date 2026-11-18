"""실제 이미지·스트림을 사용해 내부 재시도, 선택, 이력·학생 버전 경계를 검증한다."""

from contextlib import closing, contextmanager
from io import BytesIO
import json
from pathlib import Path
import sqlite3
from threading import Event
from urllib.error import HTTPError, URLError

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
import pytest

from app import ocr_jobs
from app.ocr import llama
from app.ocr.llama import OCRError
from app.ocr.pipeline import OCRPipeline
from app.ocr.retry import assess_result, critical_signature, select_result
from app.submission_api import create_submission_app


def result(text, status="needs_review"):
    return {"status": status, "text": text, "issues": [], "regions": []}


@pytest.mark.parametrize("marker", ["[UNREADABLE]", "[unreadable]", "[판독불가]"])
def test_unreadable_markers_trigger_retry(marker):
    assessment = assess_result(result("주어진 식 " + marker))
    assert assessment["usable"]
    assert "unreadable" in assessment["retry_reasons"]
    assert not assess_result(result(marker))["usable"]


def test_no_retry_for_clean_text_or_nonretryable_configuration():
    assert assess_result(result(r"주어진 식 \(x^2-3=0\)"))["retry_reasons"] == []
    failed = {**result("", "failed"), "retryable_error": False}
    assert assess_result(failed)["retry_reasons"] == []


@pytest.mark.parametrize("finish,reason", [
    ("length", "output_truncated"), ("limit", "incomplete_output"),
    ("repetition", "incomplete_output"), ("incomplete", "incomplete_output"),
])
def test_only_token_limit_increases_output_budget(finish, reason):
    candidate = result("", "failed")
    candidate["regions"] = [{"finish_reason": finish}]
    reasons = assess_result(candidate)["retry_reasons"]
    assert reason in reasons
    assert ("output_truncated" in reasons) is (finish == "length")


def test_observed_choice_gaps_without_assuming_five_choices():
    assert "choice_gaps" in assess_result(result("① A ③ C"))["retry_reasons"]
    assert "choice_gaps" not in assess_result(result("① A ② B ③ C"))["retry_reasons"]


@pytest.mark.parametrize("a,b", [
    ("x=-3", "-x=3"), ("x^2=3", "x^3=3"), ("x<3", "x>3"),
    ("옳지 않은 것은?", "옳은 것은?"), ("x=3", "y=3"),
])
def test_critical_differences_include_positions_and_variables(a, b):
    assert critical_signature(a) != critical_signature(b)


def test_selection_preserves_first_on_tie_and_does_not_favor_length():
    candidates = [result("x=3"), result("설명을 길게 덧붙였지만 x=3")]
    assert select_result(candidates)[0] == 0


def test_selection_keeps_usable_first_after_retry_failure():
    candidates = [result("주어진 식 [UNREADABLE] x=3"), result("", "failed")]
    selected, summary = select_result(candidates)
    assert selected == 0
    assert summary["review_warnings"][0]["code"] == "unreadable"


def test_critical_conflict_requires_source_evidence_to_replace_first():
    candidates = [result("주어진 식 x=-3 [UNREADABLE]"), result("주어진 식 x=3")]
    selected, summary = select_result(candidates)
    assert selected == 0
    assert summary["review_warnings"][-1]["code"] == "critical_mismatch"
    assert select_result(candidates, verified_index=1)[0] == 1


class SequenceBackend:
    def __init__(self, responses, entered=None, release=None):
        self.responses = responses
        self.calls = []
        self.entered, self.release = entered, release

    @contextmanager
    def connect(self):
        yield self

    def stream(self, image, prompt, max_tokens):
        index = len(self.calls)
        self.calls.append({"image": image, "prompt": prompt, "tokens": max_tokens})
        if index == 1 and self.entered:
            self.entered.set()
            assert self.release.wait(10)
        response = self.responses[index]
        if isinstance(response, Exception):
            raise response
        text, finish = response
        yield {"type": "delta", "text": text}
        yield {"type": "complete", "text": text, "finish_reason": finish}


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "retry.sqlite3"
    migrations = Path(__file__).resolve().parents[1] / "migrations"
    with closing(sqlite3.connect(path)) as connection:
        for name in ("001_problem_bank.sqlite.sql", "002_problem_classification.sqlite.sql",
                     "004_problem_submissions.sqlite.sql", "005_problem_ocr_jobs.sqlite.sql"):
            connection.executescript((migrations / name).read_text(encoding="utf-8"))
    return path


def photo():
    output = BytesIO()
    image = Image.new("RGB", (120, 80), (175, 175, 175))
    ImageDraw.Draw(image).rectangle((8, 8, 50, 40), fill=(40, 40, 40))
    image.save(output, "PNG")
    return output.getvalue()


HEADERS = {"Authorization": "Bearer retry-test"}


def start(client):
    uploaded = client.post("/submissions/photo", headers=HEADERS,
                           files={"files": ("math.png", photo(), "image/png")})
    assert uploaded.status_code == 201, uploaded.text
    sid = uploaded.json()["id"]
    started = client.post(f"/submissions/{sid}/ocr", headers=HEADERS)
    assert started.status_code == 202, started.text
    return sid, started.json()["id"]


def run(database, backend, condition_extractor=None):
    return create_submission_app(
        database, dev_token="retry-test", owner_id="A",
        pipeline_factory=lambda: OCRPipeline(
            backend, primary_engine="qwen", condition_extractor=condition_extractor,
        ),
    )


def drain(app):
    app.state.ocr_jobs.executor.submit(lambda: None).result(timeout=10)


def test_truncated_retry_increases_tokens_and_delays_student_version(database):
    entered, release = Event(), Event()
    backend = SequenceBackend([("중간까지", "length"), ("문제 x=-3", "stop")], entered, release)
    app = run(database, backend)
    with TestClient(app) as client:
        sid, job_id = start(client)
        try:
            assert entered.wait(5)
            assert client.get(f"/submissions/{sid}", headers=HEADERS).json()["latest_input"] is None
            history = client.get(f"/submissions/{sid}/ocr/history", headers=HEADERS).json()
            assert len(history) == 1
        finally:
            release.set()
        drain(app)
        current = client.get(f"/submissions/{sid}", headers=HEADERS).json()
        assert current["latest_input"]["content_text"] == "문제 x=-3"
        assert current["latest_input"]["version_number"] == 1
        assert current["ocr_review"]["selected_attempt"] == 2
        history = client.get(f"/submissions/{sid}/ocr/history", headers=HEADERS).json()
        assert len(history) == 2
        assert history[0]["raw_response"]["regions"][0]["text"] == "중간까지"
        assert [call["tokens"] for call in backend.calls] == [1024, 2048]
        assert backend.calls[0]["image"] == backend.calls[1]["image"]
        events = client.get(f"/ocr-jobs/{job_id}/events", headers=HEADERS).text
        assert '"stage": "retry"' in events
        assert events.count("event: completed") == 1
        assert client.post(f"/submissions/{sid}/ocr", headers=HEADERS).status_code == 409


def test_unreadable_retry_changes_copy_and_runs_conditions_once(database):
    backend = SequenceBackend([("문제 x=3 [UNREADABLE]", "stop"), ("문제 x=3", "stop")])

    class Conditions:
        calls = 0

        def extract(self, image):
            self.calls += 1
            return {"status": "completed", "conditions": []}

    conditions = Conditions()
    app = run(database, backend, conditions)
    with TestClient(app) as client:
        sid, _ = start(client)
        drain(app)
        current = client.get(f"/submissions/{sid}", headers=HEADERS).json()
        assert current["latest_input"]["content_text"] == "문제 x=3"
        assert current["ocr_review"]["review_warnings"] == []
        assert backend.calls[0]["image"] != backend.calls[1]["image"]
        assert conditions.calls == 1


@pytest.mark.parametrize("choice,expected,warning", [("second", 2, False), ("uncertain", 1, True)])
def test_conflicting_numbers_are_verified_and_restored(database, choice, expected, warning):
    backend = SequenceBackend([
        ("문제 x=-3 [UNREADABLE]", "stop"), ("문제 x=3", "stop"),
        (json.dumps({"choice": choice}), "stop"),
    ])
    app = run(database, backend)
    with TestClient(app) as client:
        sid, _ = start(client)
        drain(app)
        current = client.get(f"/submissions/{sid}", headers=HEADERS).json()
        assert current["ocr_review"]["selected_attempt"] == expected
        assert any(w["code"] == "critical_mismatch" for w in current["ocr_review"]["review_warnings"]) is warning
        assert len(backend.calls) == 3  # 전사 두 번과 원본 대조 한 번.
        saved = client.patch(f"/submissions/{sid}/input", headers=HEADERS,
                             json={"content_text": "학생 수정", "expected_version_number": 1})
        assert saved.status_code == 200
        assert saved.json()["ocr_review"] == current["ocr_review"]
    with TestClient(run(database, backend)) as fresh:
        restored = fresh.get(f"/submissions/{sid}", headers=HEADERS).json()
        assert restored["ocr_review"] == current["ocr_review"]
        assert restored["latest_input"]["content_text"] == "학생 수정"


@pytest.mark.parametrize("retryable,calls", [(False, 1), (True, 2)])
def test_connection_retry_is_bounded(database, monkeypatch, retryable, calls):
    monkeypatch.setattr(ocr_jobs, "sleep", lambda seconds: None)
    backend = SequenceBackend([OCRError("fixture", retryable=retryable)] * 2)
    app = run(database, backend)
    with TestClient(app) as client:
        sid, job_id = start(client)
        drain(app)
        assert len(backend.calls) == calls
        assert client.get(f"/submissions/{sid}", headers=HEADERS).json()["latest_input"] is None
        assert client.get(f"/ocr-jobs/{job_id}", headers=HEADERS).json()["status"] == "failed"


def test_retry_budget_preserves_first_with_warning(database, monkeypatch):
    monkeypatch.setattr(ocr_jobs, "OCR_BUDGET_SECONDS", 0)
    backend = SequenceBackend([("문제 x=3 [UNREADABLE]", "stop")])
    app = run(database, backend)
    with TestClient(app) as client:
        sid, _ = start(client)
        drain(app)
        current = client.get(f"/submissions/{sid}", headers=HEADERS).json()
        assert len(backend.calls) == 1
        assert current["ocr_review"]["retry_skipped"] == "time_budget"
        assert current["ocr_review"]["review_warnings"][0]["code"] == "unreadable"


def test_verifier_failure_keeps_first_and_exposes_conflict(database):
    backend = SequenceBackend([
        ("문제 x=-3 [UNREADABLE]", "stop"), ("문제 x=3", "stop"), OCRError("fixture"),
    ])
    app = run(database, backend)
    with TestClient(app) as client:
        sid, _ = start(client)
        drain(app)
        current = client.get(f"/submissions/{sid}", headers=HEADERS).json()
        assert current["latest_input"]["content_text"] == "문제 x=-3 [UNREADABLE]"
        assert current["ocr_review"]["verification"] == "unavailable"
        assert current["ocr_review"]["review_warnings"][-1]["code"] == "critical_mismatch"
        history = client.get(f"/submissions/{sid}/ocr/history", headers=HEADERS).json()
        assert len(history) == 2


def test_only_unreadable_is_not_a_student_draft(database):
    backend = SequenceBackend([("[UNREADABLE]", "stop")] * 2)
    app = run(database, backend)
    with TestClient(app) as client:
        sid, job_id = start(client)
        drain(app)
        assert len(backend.calls) == 2
        assert client.get(f"/submissions/{sid}", headers=HEADERS).json()["latest_input"] is None
        assert client.get(f"/ocr-jobs/{job_id}", headers=HEADERS).json()["status"] == "failed"


@pytest.mark.parametrize("error,retryable", [
    (HTTPError("http://localhost", 400, "bad request", None, None), False),
    (HTTPError("http://localhost", 401, "unauthorized", None, None), False),
    (HTTPError("http://localhost", 503, "unavailable", None, None), True),
    (URLError(ConnectionRefusedError("not running")), False),
    (TimeoutError("timeout"), True),
])
def test_transport_errors_distinguish_configuration(monkeypatch, error, retryable):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(llama, "urlopen", fail)
    with pytest.raises(OCRError) as caught:
        list(llama.LlamaClient("http://localhost:8081").stream(b"image", "OCR", 100))
    assert caught.value.retryable is retryable
