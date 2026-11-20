"""모델을 실행하지 않고 서버 모델 선택·기존 SSE 전사 계약을 검사한다."""

from io import BytesIO
import importlib.util
import json
from pathlib import Path

import pytest
from PIL import Image

from app.ocr import Backend
from app.ocr import llama
from app.ocr import quality
from app.ocr_jobs import OCRJobManager, build_qwen_pipeline


MODEL = "qwen3-vl:4b-instruct-q4_K_M"


@pytest.mark.parametrize("model_name", [None, MODEL])
def test_remote_model_selection_keeps_image_text_and_stream_contract(monkeypatch, model_name):
    requests = []
    text = r"식 \(3(x-2)=12\)을 푸시오."

    def respond(request, *, timeout):
        requests.append(request)
        assert timeout == 17
        frames = [
            {"choices": [{"delta": {"content": text}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 12, "completion_tokens": 9}},
        ]
        return BytesIO(("".join(f"data: {json.dumps(frame)}\n\n" for frame in frames)
                        + "data: [DONE]\n\n").encode())

    monkeypatch.setattr(llama, "urlopen", respond)
    with Backend(url="http://127.0.0.1:11434", timeout=17, model_name=model_name).connect() as client:
        events = list(client.stream(b"synthetic image", "그대로 전사", 128))
    assert len(requests) == 1
    assert requests[0].full_url == "http://127.0.0.1:11434/v1/chat/completions"
    payload = json.loads(requests[0].data)
    if model_name is None:
        assert "model" not in payload
    else:
        assert payload["model"] == model_name
    content = payload["messages"][0]["content"]
    assert content[0]["image_url"]["url"] == "data:image/png;base64,c3ludGhldGljIGltYWdl"
    assert content[1]["text"] == "그대로 전사"
    assert payload["stream"] is True
    assert payload["max_tokens"] == 128
    assert events[0] == {"type": "delta", "text": text}
    assert events[-1]["text"] == text
    assert events[-1]["finish_reason"] == "stop"
    assert events[-1]["usage"]["completion_tokens"] == 9


@pytest.mark.parametrize("model_name", ["", "   ", 42])
def test_invalid_remote_model_names_are_rejected_without_connecting(model_name):
    with pytest.raises(ValueError, match="모델 이름"):
        Backend(url="http://127.0.0.1:11434/v1", model_name=model_name)


@pytest.mark.parametrize("configured", [None, "", "   ", MODEL])
def test_pipeline_and_metadata_use_same_configured_model(monkeypatch, tmp_path, configured):
    if configured is None:
        monkeypatch.delenv("DIDGO_QWEN_MODEL", raising=False)
    else:
        monkeypatch.setenv("DIDGO_QWEN_MODEL", configured)
    monkeypatch.setenv("DIDGO_QWEN_URL", "http://127.0.0.1:11434/v1")
    pipeline = build_qwen_pipeline()
    manager = OCRJobManager(tmp_path / "unused.sqlite3")
    expected = MODEL if configured == MODEL else None
    assert pipeline.paddle.model_name == expected
    assert pipeline.paddle.url == "http://127.0.0.1:11434/v1"
    assert pipeline.condition_extractor.backend.model_name == expected
    assert pipeline.condition_extractor.backend.url == pipeline.paddle.url
    assert manager.model_name == (expected or "Qwen3-VL-4B-Instruct")
    assert pipeline.primary_engine == "qwen"
    assert pipeline.orientation_detector is not None
    assert pipeline.layout_detector is not None


def test_explicit_metadata_model_overrides_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("DIDGO_QWEN_MODEL", MODEL)
    manager = OCRJobManager(tmp_path / "unused.sqlite3", model_name="explicit-model")
    assert manager.model_name == "explicit-model"


def test_photo_quality_uses_configured_remote_model(monkeypatch):
    requested = []

    class Client:
        def __init__(self, url, **options):
            requested.append((url, options))

        def stream(self, *_args, **_options):
            yield {"type": "complete", "finish_reason": "stop", "text": json.dumps({
                "content_occluded": False, "content_cut_off": False,
            })}

    monkeypatch.setenv("DIDGO_QWEN_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.setenv("DIDGO_QWEN_MODEL", MODEL)
    monkeypatch.setattr(quality, "LlamaClient", Client)
    monkeypatch.setattr(quality, "inspect_photo_quality", lambda _content: {
        "image_size": [640, 640], "analysis_size": [640, 640],
        "brightness_mean": 180, "bright_pixel_ratio": 0.1, "laplacian_variance": 100,
    })
    output = BytesIO()
    with Image.new("RGB", (8, 8), "white") as picture:
        picture.save(output, format="PNG")
    result = quality.check_photo_quality(output.getvalue())
    assert result["content_check"]["status"] == "checked"
    assert requested == [("http://127.0.0.1:11434/v1", {"timeout": 15, "model_name": MODEL})]


def test_live_check_cli_passes_remote_model_without_running_it(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "scripts/check_live_ocr_stream.py"
    spec = importlib.util.spec_from_file_location("remote_model_live_check", path)
    command = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(command)
    captured = []

    class StopBeforeRequests(Exception):
        pass

    def capture_app(_database, *, pipeline_factory, **_options):
        pipeline_factory()
        raise StopBeforeRequests

    monkeypatch.setenv("DIDGO_QWEN_MODEL", "environment-model")
    monkeypatch.setattr(command, "create_submission_app", capture_app)
    monkeypatch.setattr(command, "OCRPipeline", lambda backend, **_options: captured.append(backend))
    monkeypatch.setattr("sys.argv", [
        "check_live_ocr_stream.py", "--qwen-url", "http://127.0.0.1:11434/v1",
        "--qwen-model-name", MODEL,
    ])
    with pytest.raises(StopBeforeRequests):
        command.main()
    assert captured[0].model_name == MODEL
    assert captured[0].url == "http://127.0.0.1:11434/v1"


def test_live_check_rejects_remote_name_with_local_model_files(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "scripts/check_live_ocr_stream.py"
    spec = importlib.util.spec_from_file_location("remote_model_conflict_check", path)
    command = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(command)
    monkeypatch.setattr("sys.argv", [
        "check_live_ocr_stream.py", "--qwen-model", "unused.gguf",
        "--qwen-mmproj", "unused-mmproj.gguf", "--qwen-model-name", MODEL,
    ])
    with pytest.raises(SystemExit) as error:
        command.main()
    assert error.value.code == 2
