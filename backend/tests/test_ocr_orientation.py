"""모델 다운로드 없이 방향 합의·회전·EXIF·불확실성 처리를 검증한다."""

from contextlib import contextmanager
import importlib.util
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

PIL_AVAILABLE = importlib.util.find_spec("PIL") is not None
if PIL_AVAILABLE:
    from PIL import Image, ImageDraw
    from app.ocr.images import Region, open_image, prepare
    from app.ocr.orientation import ANGLES, DocumentOrientationDetector, combine_predictions
    from app.ocr.pipeline import OCRPipeline


def predictions_for(angle, score=0.94):
    return [
        {"label_names": [str(a) for a in ANGLES],
         "scores": [score if a == (angle + cw) % 360 else (1 - score) / 3 for a in ANGLES]}
        for cw in ANGLES
    ]


@unittest.skipUnless(PIL_AVAILABLE, "Pillow가 필요합니다.")
class OrientationDecisionTests(unittest.TestCase):
    def test_all_four_input_rotations_map_back_to_original_coordinates(self):
        for angle in ANGLES:
            with self.subTest(angle=angle):
                result = combine_predictions(predictions_for(angle))
                self.assertEqual(result["rotation_ccw"], angle)
                self.assertEqual(result["votes"], 4)
                self.assertEqual(result["status"], "corrected" if angle else "unchanged")

    def test_low_score_and_disagreement_never_rotate(self):
        low = combine_predictions(predictions_for(90, 0.6))
        self.assertEqual(low["status"], "uncertain")
        self.assertEqual(low["rotation_ccw"], 0)

        records = predictions_for(90, 0.99)
        records[0] = {"label_names": ["0", "90", "180", "270"], "scores": [0.51, 0.49, 0, 0]}
        split = combine_predictions(records)
        self.assertGreater(split["score"], 0.8)
        self.assertEqual(split["votes"], 3)
        self.assertEqual(split["rotation_ccw"], 0)
        self.assertEqual(split["status"], "uncertain")

    def test_invalid_predictions_are_rejected(self):
        for invalid in ([], predictions_for(0)[:3]):
            with self.assertRaises(ValueError):
                combine_predictions(invalid)
        records = predictions_for(90)
        records[0]["scores"][0] = float("nan")
        with self.assertRaises(ValueError):
            combine_predictions(records)

    def test_small_and_blank_images_do_not_load_model(self):
        detector = DocumentOrientationDetector("unused")
        with patch.object(detector, "_load", side_effect=AssertionError("must not load")):
            self.assertEqual(detector.detect(Image.new("RGB", (130, 33), "white"))["reason"], "too_small")
            blank = detector.detect(Image.new("RGB", (300, 200), "white"))
            self.assertEqual(blank["reason"], "low_contrast")
            self.assertEqual(blank["rotation_ccw"], 0)

    def test_model_is_loaded_only_once(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in ("inference.json", "inference.yml", "inference.pdiparams"):
                (Path(directory) / name).write_bytes(b"fixture")
            factory = Mock(return_value=object())
            with patch.dict("sys.modules", {"paddleocr": SimpleNamespace(DocImgOrientationClassification=factory)}):
                detector = DocumentOrientationDetector(directory)
                self.assertIs(detector._load(), detector._load())
            factory.assert_called_once()


@unittest.skipUnless(PIL_AVAILABLE, "Pillow가 필요합니다.")
class OrientationPipelineTests(unittest.TestCase):
    def setUp(self):
        image = Image.new("RGB", (240, 180), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((15, 20, 75, 50), fill="red")
        draw.rectangle((155, 110, 225, 160), fill="blue")
        output = io.BytesIO()
        image.save(output, format="PNG")
        self.content = output.getvalue()

    def backend(self):
        owner = self
        class Backend:
            @contextmanager
            def connect(self):
                yield self
            def stream(self, image, prompt, max_tokens):
                owner.ocr_input = image
                yield {"type": "complete", "text": "문제 본문", "finish_reason": "stop"}
        return Backend()

    def test_ocr_receives_corrected_image_and_records_direction(self):
        detector = Mock()
        detector.detect.return_value = {"status": "corrected", "rotation_ccw": 90, "score": 0.95}
        events = list(OCRPipeline(self.backend(), primary_engine="qwen", orientation_detector=detector).stream(self.content))
        result = events[-1]["result"]
        expected = open_image(self.content).rotate(90, expand=True)
        expected_bytes, _ = prepare(expected, Region("problem"))
        self.assertEqual(self.ocr_input, expected_bytes)
        self.assertEqual(result["rotation_ccw"], 90)
        self.assertEqual(result["image_size"], [180, 240])
        self.assertEqual(result["status"], "needs_review")
        self.assertTrue(any(e["type"] == "orientation" for e in events))

    def test_exif_is_applied_before_orientation_detection(self):
        with Image.open(io.BytesIO(self.content)) as image:
            exif = image.getexif()
            exif[274] = 6
            output = io.BytesIO()
            image.save(output, format="JPEG", exif=exif)
        detector = Mock()
        detector.detect.return_value = {"status": "unchanged", "rotation_ccw": 0}
        result = OCRPipeline(self.backend(), primary_engine="qwen", orientation_detector=detector).recognize(output.getvalue())
        self.assertEqual(detector.detect.call_args.args[0].size, (180, 240))
        self.assertEqual(result["image_size"], [180, 240])

    def test_uncertain_direction_keeps_image_and_requires_review(self):
        detector = Mock()
        detector.detect.return_value = {"status": "uncertain", "rotation_ccw": 0, "predicted_rotation_ccw": 180}
        result = OCRPipeline(self.backend(), primary_engine="qwen", orientation_detector=detector).recognize(self.content)
        expected, _ = prepare(open_image(self.content), Region("problem"))
        self.assertEqual(self.ocr_input, expected)
        self.assertEqual(result["rotation_ccw"], 0)
        self.assertTrue(any("방향" in issue for issue in result["issues"]))

    def test_manual_rotation_and_explicit_regions_skip_auto_detection(self):
        detector = Mock()
        detector.detect.side_effect = AssertionError("explicit coordinates must not change")
        for options, regions in (({"rotation": 90}, None), ({}, [Region("body")])):
            result = OCRPipeline(self.backend(), primary_engine="qwen", orientation_detector=detector, **options).recognize(self.content, regions)
            self.assertEqual(result["orientation"]["status"], "skipped")
            self.assertEqual(result["rotation_ccw"], options.get("rotation", 0))
        detector.detect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
