"""Git으로 받은 완성 DB만으로 실행하고 개인 데이터가 보존되는지 검사한다."""

from contextlib import closing
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prepare_problem_bank.py"
spec = importlib.util.spec_from_file_location("prepare_problem_bank", SCRIPT)
distribution = importlib.util.module_from_spec(spec)
spec.loader.exec_module(distribution)
SEED = distribution.DATA_DIR / "problem_bank.seed.sqlite3"
MANIFEST_PATH = distribution.DATA_DIR / "problem_bank.seed.json"


class ProblemDistributionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    def tearDown(self):
        self.temp.cleanup()

    def test_clean_checkout_works_without_zip_sources_or_dependencies(self):
        script = self.folder / "backend" / "scripts" / SCRIPT.name
        data = self.folder / "data" / "problem_bank"
        script.parent.mkdir(parents=True)
        data.mkdir(parents=True)
        shutil.copyfile(SCRIPT, script)
        shutil.copyfile(SEED, data / SEED.name)
        shutil.copyfile(MANIFEST_PATH, data / MANIFEST_PATH.name)
        result = subprocess.run([sys.executable, "-B", str(script)], cwd=self.folder,
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["action"], "copied_shared_database")
        self.assertEqual(report["questions"], 30339)
        self.assertEqual(report["topics"], 1165)
        self.assertEqual(report["benchmark_items"], 50)
        self.assertFalse((data / "sources").exists())

    def test_repeated_prepare_preserves_local_upload_and_shared_database(self):
        local = self.folder / "local.sqlite3"
        distribution.prepare(SEED, local, self.manifest)
        with closing(sqlite3.connect(local)) as connection:
            filename, sha, image = connection.execute(
                "SELECT filename, sha256, content FROM benchmark_assets LIMIT 1").fetchone()
            connection.execute(
                "INSERT INTO uploaded_problems (original_filename, mime_type, image_sha256, image_content) VALUES (?, 'image/png', ?, ?)",
                (filename, sha, image),
            )
            connection.commit()
        before = hashlib.sha256(local.read_bytes()).hexdigest()
        report = distribution.prepare(SEED, local, self.manifest)
        self.assertEqual(report["action"], "kept_existing_database")
        self.assertEqual(report["uploaded_problems"], 1)
        self.assertEqual(hashlib.sha256(local.read_bytes()).hexdigest(), before)
        self.assertEqual(hashlib.sha256(SEED.read_bytes()).hexdigest(), self.manifest["sha256"])

    def test_existing_empty_health_check_database_is_initialized(self):
        local = self.folder / "health.db"
        with closing(sqlite3.connect(local)) as connection:
            connection.execute("SELECT 1")
        report = distribution.prepare(SEED, local, self.manifest)
        self.assertEqual(report["action"], "initialized_empty_database")
        self.assertEqual(report["questions"], 30339)

    def test_unrelated_existing_database_is_not_overwritten(self):
        local = self.folder / "other.db"
        with closing(sqlite3.connect(local)) as connection:
            connection.execute("CREATE TABLE unrelated (value TEXT)")
            connection.execute("INSERT INTO unrelated VALUES ('preserve')")
            connection.commit()
        before = hashlib.sha256(local.read_bytes()).hexdigest()
        with self.assertRaises(sqlite3.Error):
            distribution.prepare(SEED, local, self.manifest)
        self.assertEqual(hashlib.sha256(local.read_bytes()).hexdigest(), before)

    def test_bad_checksum_rejected_without_creating_database(self):
        wrong = copy.deepcopy(self.manifest)
        wrong["sha256"] = "0" * 64
        local = self.folder / "bad.sqlite3"
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            distribution.prepare(SEED, local, wrong)
        self.assertFalse(local.exists())

    def test_shared_database_cannot_be_used_as_writable_target(self):
        with self.assertRaisesRegex(ValueError, "공유 원본"):
            distribution.prepare(SEED, SEED, self.manifest)


if __name__ == "__main__":
    unittest.main()
