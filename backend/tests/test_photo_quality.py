"""품질 경고 기준, 모델 응답 실패, 업로드 API 연결을 검증한다."""

from contextlib import closing
from io import BytesIO
import json
from pathlib import Path
import sqlite3

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from app import submission_api
from app.ocr import quality
from app.ocr.llama import OCRError


def photo_bytes():
    output = BytesIO()
    Image.new("RGB", (960, 1280), (175, 175, 175)).save(output, "PNG")
    return output.getvalue()


@pytest.fixture
def normal_metrics():
    return {
        "image_size": [3000, 4000],
        "analysis_size": [960, 1280],
        "brightness_mean": 173.09,
        "dark_pixel_ratio": 0,
        "bright_pixel_ratio": 0,
        "laplacian_variance": 103.207,
    }


@pytest.fixture
def measured_photo(monkeypatch, normal_metrics):
    monkeypatch.setattr(
        quality, "inspect_photo_quality", lambda content: normal_metrics
    )
    return photo_bytes()


def model_response(monkeypatch, text=None, finish="stop", error=None):
    class FixtureClient:
        def __init__(self, *args, **kwargs):
            pass

        def stream(self, *args, **kwargs):
            if error is not None:
                raise error
            if text is not None:
                yield {"type": "complete", "text": text, "finish_reason": finish}

    monkeypatch.setattr(quality, "LlamaClient", FixtureClient)


@pytest.mark.parametrize(
    "sharpness,expected",
    [(19.014, True), (29.999, True), (30, False), (43.707, False), (103.207, False)],
)
def test_blur_threshold(normal_metrics, sharpness, expected):
    normal_metrics["laplacian_variance"] = sharpness
    result = quality.evaluate_photo_quality(normal_metrics)
    codes = [warning["code"] for warning in result["warnings"]]
    assert ("possibly_blurred" in codes) is expected
    assert result["retake_required"] is expected


def test_basic_warning_skips_model(monkeypatch, normal_metrics, measured_photo):
    normal_metrics["laplacian_variance"] = 19.014

    def unexpected_client(*args, **kwargs):
        pytest.fail("A blurred photo should not need a model request")

    monkeypatch.setattr(quality, "LlamaClient", unexpected_client)
    result = quality.check_photo_quality(measured_photo)
    assert result["status"] == "warning"
    assert result["content_check"]["status"] == "skipped"
    assert result["warnings"][0]["code"] == "possibly_blurred"
    assert result["retake_required"] is True


@pytest.mark.parametrize(
    "occluded,cropped,expected",
    [
        (False, False, []),
        (True, False, ["possibly_occluded"]),
        (False, True, ["possibly_cropped"]),
        (True, True, ["possibly_occluded", "possibly_cropped"]),
    ],
)
def test_content_warnings(monkeypatch, measured_photo, occluded, cropped, expected):
    model_response(monkeypatch, json.dumps({
        "blur": True,  # 모델의 흐림 추측 대신 측정한 선명도를 사용한다.
        "content_occluded": occluded,
        "content_cut_off": cropped,
    }))
    result = quality.check_photo_quality(measured_photo)
    assert [warning["code"] for warning in result["warnings"]] == expected
    assert result["status"] == ("warning" if expected else "no_warning")
    assert result["retake_required"] is bool(expected)
    assert result["content_check"] == {
        "status": "checked",
        "content_occluded": occluded,
        "content_cut_off": cropped,
    }


@pytest.mark.parametrize(
    "text,finish",
    [
        ("not json", "stop"),
        ('{"content_occluded": "false", "content_cut_off": false}', "stop"),
        ('{"content_occluded": false}', "stop"),
        ("[]", "stop"),
        (None, "stop"),
        ('{"content_occluded": false, "content_cut_off": false}', "length"),
    ],
)
def test_invalid_response_requires_review(monkeypatch, measured_photo, text, finish):
    model_response(monkeypatch, text, finish)
    result = quality.check_photo_quality(measured_photo)
    assert result["status"] == "warning"
    assert result["content_check"]["status"] == "unavailable"
    assert result["warnings"][0]["code"] == "quality_check_unavailable"
    assert result["retake_required"] is False


def test_server_failure_requires_review(monkeypatch, measured_photo):
    model_response(monkeypatch, error=OCRError("fixture connection failure"))
    result = quality.check_photo_quality(measured_photo)
    assert result["status"] == "warning"
    assert result["warnings"][0]["code"] == "quality_check_unavailable"
    assert result["retake_required"] is False


@pytest.fixture
def quality_database(tmp_path):
    database = tmp_path / "quality.sqlite3"
    migrations = Path(__file__).resolve().parents[1] / "migrations"
    with closing(sqlite3.connect(database)) as connection:
        for name in (
            "001_problem_bank.sqlite.sql",
            "002_problem_classification.sqlite.sql",
            "004_problem_submissions.sqlite.sql",
            "005_problem_ocr_jobs.sqlite.sql",
        ):
            connection.executescript((migrations / name).read_text(encoding="utf-8"))
    return database


def test_upload_preserves_advisory_warning_and_photo(quality_database, monkeypatch):
    expected = {
        "policy_version": "experimental-v3",
        "status": "warning",
        "retake_required": False,
        "warnings": [{"code": "quality_check_unavailable", "message": "직접 확인"}],
        "content_check": {"status": "unavailable"},
    }
    content = photo_bytes()

    def inspect_uploaded_photo(uploaded):
        assert uploaded == content
        return expected

    monkeypatch.setattr(submission_api, "check_photo_quality", inspect_uploaded_photo)
    app = submission_api.create_submission_app(
        quality_database, dev_token="quality-test", owner_id="student_A"
    )
    headers = {"Authorization": "Bearer quality-test"}
    with TestClient(app) as client:
        response = client.post(
            "/submissions/photo",
            headers=headers,
            files={"files": ("math.png", content, "image/png")},
        )
        assert response.status_code == 201, response.text
        assert response.json()["photo_quality"] == expected
        photo = client.get(
            f"/submissions/{response.json()['id']}/photo", headers=headers
        )
        assert photo.status_code == 200
        assert photo.content == content


@pytest.mark.parametrize(
    "code", ["possibly_blurred", "possibly_occluded", "possibly_cropped"]
)
def test_retake_photo_has_no_submission_or_ocr_job(quality_database, monkeypatch, code):
    rejected = {
        "policy_version": "experimental-v3",
        "status": "warning",
        "retake_required": True,
        "warnings": [{"code": code, "message": "사진을 다시 촬영해 주세요."}],
    }
    monkeypatch.setattr(submission_api, "check_photo_quality", lambda content: rejected)
    app = submission_api.create_submission_app(
        quality_database, dev_token="quality-test", owner_id="student_A"
    )
    headers = {"Authorization": "Bearer quality-test"}
    with TestClient(app) as client:
        response = client.post(
            "/submissions/photo", headers=headers,
            files={"files": ("unreadable.png", photo_bytes(), "image/png")},
        )
        assert response.status_code == 422, response.text
        assert response.json()["detail"]["code"] == "photo_retake_required"
        assert response.json()["detail"]["photo_quality"] == rejected
        assert "id" not in response.json()
        with closing(sqlite3.connect(quality_database)) as connection:
            for table in ("uploaded_problems", "problem_submissions", "problem_ocr_jobs"):
                assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        assert client.post("/submissions/1/ocr", headers=headers).status_code == 404

        accepted = {
            "policy_version": "experimental-v3",
            "status": "no_warning",
            "retake_required": False,
            "warnings": [],
        }
        monkeypatch.setattr(submission_api, "check_photo_quality", lambda content: accepted)
        retaken = client.post(
            "/submissions/photo", headers=headers,
            files={"files": ("retaken.png", photo_bytes(), "image/png")},
        )
        assert retaken.status_code == 201, retaken.text
        assert retaken.json()["photo_quality"]["retake_required"] is False


def test_warning_code_cannot_bypass_retake_with_false_flag(quality_database, monkeypatch):
    monkeypatch.setattr(submission_api, "check_photo_quality", lambda content: {
        "status": "warning",
        "retake_required": False,
        "warnings": [{"code": "possibly_blurred", "message": "흐림"}],
    })
    app = submission_api.create_submission_app(
        quality_database, dev_token="quality-test", owner_id="student_A"
    )
    with TestClient(app) as client:
        response = client.post(
            "/submissions/photo",
            headers={"Authorization": "Bearer quality-test"},
            files={"files": ("blurred.png", photo_bytes(), "image/png")},
        )
        assert response.status_code == 422
        assert response.json()["detail"]["photo_quality"]["retake_required"] is True
