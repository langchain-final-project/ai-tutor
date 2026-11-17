"""원천 ZIP의 이미지와 라벨을 보조 DB를 통해 읽는다. 기존 DB를 수정하지 않는다."""

from contextlib import closing
import hashlib
import io
import json
import math
from pathlib import Path
import sqlite3
import struct
import zipfile


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_RAW_DB = ROOT / "data/problem_bank/raw/sources.sqlite3"
DEFAULT_DATASET_ROOT = ROOT / "수학 과목 문제생성 데이터_중등"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def read_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def png_dimensions(content: bytes) -> tuple[int, int]:
    require(len(content) >= 24 and content[:8] == b"\x89PNG\r\n\x1a\n", "PNG 형식이 아닙니다.")
    require(content[12:16] == b"IHDR", "PNG IHDR이 없습니다.")
    width, height = struct.unpack(">II", content[16:24])
    require(width > 0 and height > 0, "PNG 크기가 올바르지 않습니다.")
    return width, height


def archive_path(dataset_root: Path, relative_path: str) -> Path:
    root = dataset_root.resolve()
    result = (root / relative_path).resolve()
    require(result.is_relative_to(root), "원천 폴더 밖의 ZIP을 참조할 수 없습니다.")
    return result


class RawProblemStore:
    def __init__(self, database: Path = DEFAULT_RAW_DB, dataset_root: Path = DEFAULT_DATASET_ROOT):
        self.database = Path(database)
        self.dataset_root = Path(dataset_root)

    def get_problem(self, question_id: str, split: str = "Training") -> dict:
        with closing(read_connection(self.database)) as connection:
            row = connection.execute(
                "SELECT * FROM raw_problems WHERE split = ? AND question_id = ?",
                (split, question_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"문항이 없습니다: {split}/{question_id}")
            result = dict(row)
            result["assets"] = [dict(r) for r in connection.execute(
                "SELECT * FROM source_assets WHERE split = ? AND question_id = ? ORDER BY kind",
                (split, question_id),
            )]
            return result

    def read_image(self, question_id: str, kind: str = "question", split: str = "Training") -> bytes:
        require(kind in ("question", "answer"), "이미지 종류는 question 또는 answer여야 합니다.")
        with closing(read_connection(self.database)) as connection:
            row = connection.execute(
                """SELECT a.*, s.relative_path FROM source_assets a
                   JOIN source_archives s USING (archive_key)
                   WHERE a.split = ? AND a.question_id = ? AND a.kind = ?""",
                (split, question_id, kind),
            ).fetchone()
        if row is None:
            raise KeyError(f"이미지 연결이 없습니다: {split}/{question_id}/{kind}")
        with zipfile.ZipFile(archive_path(self.dataset_root, row["relative_path"])) as archive:
            content = archive.read(row["member"])
        require(len(content) == row["bytes"], "원천 이미지 크기가 변경됐습니다.")
        require(hashlib.sha256(content).hexdigest() == row["sha256"], "원천 이미지 SHA-256 불일치")
        require(png_dimensions(content) == (row["width"], row["height"]), "이미지 가로·세로 크기 불일치")
        return content

    def get_regions(self, question_id: str, kind: str = "question", split: str = "Training") -> list[dict]:
        require(kind in ("question", "answer"), "영역 종류는 question 또는 answer여야 합니다.")
        view = "question_regions" if kind == "question" else "answer_regions"
        with closing(read_connection(self.database)) as connection:
            regions = [dict(row) for row in connection.execute(
                f"SELECT * FROM {view} WHERE split = ? AND question_id = ? ORDER BY block_index, region_index",
                (split, question_id),
            )]
        for region in regions:
            region["bbox"] = json.loads(region.pop("bbox_json"))
        return regions

    def crop_region(
        self, question_id: str, output: Path, *, kind: str = "question", split: str = "Training",
        block_index: int = 0, region_index: int = 0,
    ) -> dict:
        # 이미지 연결/라벨 조회에는 Pillow가 필요 없다. 실제 자르기에서만 사용한다.
        try:
            from PIL import Image
        except ImportError as error:
            raise RuntimeError("영역 자르기에는 Pillow가 필요합니다: python -m pip install Pillow") from error
        region = next((r for r in self.get_regions(question_id, kind, split)
                       if r["block_index"] == block_index and r["region_index"] == region_index), None)
        if region is None:
            raise KeyError("선택한 영역이 없습니다.")
        bbox = region["bbox"]
        require(isinstance(bbox, list) and len(bbox) == 4, "bbox는 좌표 4개여야 합니다.")
        require(all(isinstance(v, (int, float)) and math.isfinite(v) for v in bbox), "유효하지 않은 좌표")
        require(bbox[2] > bbox[0] and bbox[3] > bbox[1], "영역의 가로·세로 길이는 양수여야 합니다.")
        with Image.open(io.BytesIO(self.read_image(question_id, kind, split))) as image:
            requested = (math.floor(bbox[0]), math.floor(bbox[1]), math.ceil(bbox[2]), math.ceil(bbox[3]))
            applied = (max(0, requested[0]), max(0, requested[1]), min(image.width, requested[2]), min(image.height, requested[3]))
            require(applied[2] > applied[0] and applied[3] > applied[1], "이미지 밖의 영역입니다.")
            cropped = image.crop(applied)
            output = Path(output)
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("xb") as stream:
                cropped.save(stream, format="PNG")
        return {"question_id": question_id, "split": split, "kind": kind,
                "region_type": region["region_type"], "text": region["text"],
                "requested_bbox": bbox, "applied_bbox": list(applied),
                "clamped": applied != requested, "output": str(output)}
