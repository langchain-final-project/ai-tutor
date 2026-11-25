"""방향 보정 표시와 원본·학생 수정 이력의 연결을 확인한다."""

from contextlib import closing
from io import BytesIO
from pathlib import Path
import sqlite3
import tempfile
import unittest

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
from app.ocr.images import open_image
from app.submission_api import create_submission_app


class RotationFixture:
    def stream(self, image):
        yield {"type": "result", "result": {
            "status": "needs_review", "text": "본문", "rotation_ccw": 90,
            "orientation": {"status": "corrected", "rotation_ccw": 90, "score": 0.95},
            "issues": [], "regions": [],
        }}


class CorrectedPhotoTests(unittest.TestCase):
    def test_original_is_preserved_and_corrected_view_survives_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "check.sqlite3"
            migrations = Path(__file__).resolve().parents[1] / "migrations"
            with closing(sqlite3.connect(database)) as connection:
                for name in ("001_problem_bank.sqlite.sql", "002_problem_classification.sqlite.sql", "004_problem_submissions.sqlite.sql", "005_problem_ocr_jobs.sqlite.sql"):
                    connection.executescript((migrations / name).read_text(encoding="utf-8"))
            image = Image.new("RGB", (120, 80), "white")
            ImageDraw.Draw(image).rectangle((5, 10, 40, 35), fill="red")
            exif = image.getexif()
            exif[274] = 6
            output = BytesIO()
            image.save(output, format="JPEG", exif=exif)
            raw = output.getvalue()
            headers = {"Authorization": "Bearer test-token"}
            app = create_submission_app(database, dev_token="test-token", owner_id="A", pipeline_factory=RotationFixture)
            with TestClient(app) as client:
                response = client.post("/submissions/photo", headers=headers, files={"files": ("math.jpg", raw, "image/jpeg")})
                self.assertEqual(response.status_code, 201)
                sid = response.json()["id"]
                response = client.post(f"/submissions/{sid}/ocr", headers=headers)
                self.assertEqual(response.status_code, 202)
                app.state.ocr_jobs.executor.submit(lambda: None).result(timeout=10)

                for confirmed in (False, True):
                    if confirmed:
                        response = client.post(f"/submissions/{sid}/confirm", headers=headers, json={"content_text": "수정한 본문", "expected_version_number": 1})
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(response.json()["review_status"], "confirmed")
                    current = client.get(f"/submissions/{sid}", headers=headers).json()
                    self.assertEqual(current["image_rotation_ccw"], 90)
                    self.assertEqual(current["image_orientation"]["status"], "corrected")
                    self.assertEqual(client.get(f"/submissions/{sid}/photo", headers=headers).content, raw)
                    view = client.get(f"/submissions/{sid}/photo?corrected=true", headers=headers)
                    self.assertEqual(view.status_code, 200)
                    self.assertEqual(view.headers["content-type"], "image/png")
                    with Image.open(BytesIO(view.content)) as displayed, open_image(raw) as normalized:
                        with normalized.rotate(90, expand=True) as expected:
                            self.assertEqual(displayed.size, expected.size)
                            self.assertEqual(displayed.tobytes(), expected.tobytes())

            other_app = create_submission_app(database, dev_token="test-token", owner_id="B", pipeline_factory=RotationFixture)
            with TestClient(other_app) as other:
                self.assertEqual(other.get(f"/submissions/{sid}/photo?corrected=true", headers=headers).status_code, 404)


if __name__ == "__main__":
    unittest.main()
