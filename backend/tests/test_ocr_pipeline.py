"""모델 다운로드 없이 전체 문제 전사·회전·수식 영역 분기를 검사한다."""

from contextlib import contextmanager, redirect_stderr, redirect_stdout
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


PIL_AVAILABLE = importlib.util.find_spec("PIL") is not None
if PIL_AVAILABLE:
    from PIL import Image, ImageDraw

    from app.ocr.images import Region
    from app.ocr.pipeline import OCRPipeline, PROBLEM_PROMPT


class FakeBackend:
    def __init__(self, text, finish_reason="stop"):
        self.text = text
        self.finish_reason = finish_reason
        self.prompts = []
        self.connections = 0

    @contextmanager
    def connect(self):
        self.connections += 1
        yield self

    def stream(self, image, prompt, max_tokens):
        self.prompts.append(prompt)
        yield {"type": "complete", "text": self.text,
               "finish_reason": self.finish_reason}


class MustNotConnect:
    @contextmanager
    def connect(self):
        raise AssertionError("이 입력에서는 해당 모델을 호출하면 안 됩니다.")
        yield


class Capture(io.StringIO):
    def reconfigure(self, **kwargs):
        pass


@unittest.skipUnless(PIL_AVAILABLE, "OCR 이미지 의존성 Pillow가 필요합니다.")
class OCRPipelineTests(unittest.TestCase):
    def setUp(self):
        image = Image.new("RGB", (120, 60), "white")
        ImageDraw.Draw(image).rectangle((5, 5, 20, 20), fill="black")
        stream = io.BytesIO()
        image.save(stream, format="PNG")
        self.image = stream.getvalue()

    def test_whole_problem_preserves_korean_and_math_without_formula_call(self):
        text = "다음 등식 $3x+2=-3+ax$의 조건을 구하시오.\n① 첫 번째 보기"
        backend = FakeBackend(text)
        pipeline = OCRPipeline(backend, formula=MustNotConnect(), primary_engine="qwen")

        events = list(pipeline.stream(self.image))
        result = events[-1]["result"]

        self.assertEqual(result["text"], text)
        self.assertEqual(result["regions"][0]["text"], text)
        self.assertEqual(result["regions"][0]["kind"], "text")
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(backend.prompts, [PROBLEM_PROMPT])
        self.assertTrue(any(e["type"] == "region_complete" and e["model"] == "qwen"
                            for e in events))

    def test_rotation_is_applied_before_region_coordinates(self):
        backend = FakeBackend("확인할 본문")
        pipeline = OCRPipeline(backend, primary_engine="qwen", rotation=90)

        result = pipeline.recognize(
            self.image, [Region("body", "text", (0, 80, 60, 120))],
        )

        self.assertEqual(result["image_size"], [60, 120])
        self.assertEqual(result["rotation_ccw"], 90)
        self.assertTrue(result["regions"][0]["usable"])

    def test_default_engine_keeps_existing_prompt(self):
        backend = FakeBackend("기존 본문")
        result = OCRPipeline(backend).recognize(self.image)

        self.assertEqual(backend.prompts, ["OCR:"])
        self.assertEqual(result["rotation_ccw"], 0)

    def test_explicit_formula_region_uses_formula_backend_only(self):
        formula = FakeBackend("x^{2}-6x=0")
        pipeline = OCRPipeline(MustNotConnect(), formula=formula, primary_engine="qwen")

        result = pipeline.recognize(self.image, [Region("formula", "formula")])

        self.assertEqual(formula.connections, 1)
        self.assertEqual(result["regions"][0]["text"], "x^{2}-6x=0")
        self.assertEqual(result["text"], "")

    def test_incomplete_output_is_not_exposed_as_usable_problem_text(self):
        result = OCRPipeline(
            FakeBackend("중간까지 읽은 내용", "length"), primary_engine="qwen",
        ).recognize(self.image)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["text"], "")
        self.assertFalse(result["regions"][0]["usable"])

    def test_invalid_engine_and_rotation_are_rejected(self):
        for options in ({"primary_engine": "unknown"}, {"rotation": 45}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                OCRPipeline(FakeBackend("본문"), **options)


@unittest.skipUnless(PIL_AVAILABLE, "OCR 이미지 의존성 Pillow가 필요합니다.")
class OCRCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "scripts/recognize_problem.py"
        spec = importlib.util.spec_from_file_location("recognize_problem_test", path)
        cls.command = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.command)

    def test_cli_routes_qwen_and_rotation_to_pipeline(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "photo.png"
            image.write_bytes(b"photo bytes")
            arguments = ["recognize_problem.py", str(image), "--ocr-engine", "qwen",
                         "--qwen-url", "http://127.0.0.1:8082", "--rotate", "90"]
            with patch("sys.argv", arguments), patch.object(self.command, "OCRPipeline") as factory:
                factory.return_value.stream.return_value = iter([
                    {"type": "result", "result": {"status": "needs_review", "text": "본문"}},
                ])
                with redirect_stdout(Capture()):
                    self.assertEqual(self.command.main(), 0)

                positional, keywords = factory.call_args
                self.assertEqual(positional[0].url, "http://127.0.0.1:8082")
                self.assertIsNone(positional[1])
                self.assertEqual(keywords["primary_engine"], "qwen")
                self.assertEqual(keywords["rotation"], 90)
                factory.return_value.stream.assert_called_once_with(
                    b"photo bytes", None, check_text=False,
                )

    def test_qwen_primary_rejects_duplicate_check_text_mode(self):
        arguments = ["recognize_problem.py", "unused.png", "--ocr-engine", "qwen", "--check-text"]
        with patch("sys.argv", arguments), patch.object(self.command, "OCRPipeline") as factory:
            output = Capture()
            with redirect_stdout(Capture()), redirect_stderr(output):
                self.assertEqual(self.command.main(), 2)
            factory.assert_not_called()
            self.assertIn("--check-text", output.getvalue())


if __name__ == "__main__":
    unittest.main()
