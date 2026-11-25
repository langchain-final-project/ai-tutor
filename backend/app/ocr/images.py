"""원본을 변경하지 않고 방향 보정·영역 자르기·확대 입력을 준비한다."""

from dataclasses import dataclass
import io
import math
from typing import Literal

from PIL import Image, ImageChops, ImageDraw, ImageOps, UnidentifiedImageError


@dataclass(frozen=True)
class Region:
    """bbox는 EXIF 방향 보정 후 원본의 (왼쪽, 위, 오른쪽, 아래) 픽셀 좌표다."""

    id: str
    kind: Literal["text", "table", "formula", "figure"] = "text"
    bbox: tuple[float, float, float, float] | None = None


def open_image(content: bytes) -> Image.Image:
    if not content or len(content) > 30 * 1024 * 1024:
        raise ValueError("이미지는 비어 있지 않은 30MB 이하 파일이어야 합니다.")
    try:
        source = Image.open(io.BytesIO(content))
    except (UnidentifiedImageError, Image.DecompressionBombError) as error:
        raise ValueError("지원하는 크기의 올바른 이미지 파일을 입력해 주세요.") from error
    with source:
        if source.width * source.height > 20_000_000:
            raise ValueError("이미지를 2천만 픽셀 이하로 줄이거나 문제 영역을 선택해 주세요.")
        source = ImageOps.exif_transpose(source).convert("RGBA")
        background = Image.new("RGBA", source.size, "white")
        return Image.alpha_composite(background, source).convert("RGB")


def region_box(image: Image.Image, region: Region) -> tuple[int, int, int, int]:
    if (not isinstance(region.id, str) or not region.id.strip()
            or not isinstance(region.kind, str) or region.kind not in {"text", "table", "formula", "figure"}):
        raise ValueError("영역 ID와 종류를 확인해 주세요.")
    if region.bbox is None:
        return (0, 0, image.width, image.height)
    b = region.bbox
    if len(b) != 4 or any(isinstance(x, bool) or not isinstance(x, (int, float))
                          or not math.isfinite(x) for x in b):
        raise ValueError("영역 좌표는 유한한 숫자 4개여야 합니다.")
    if not (0 <= b[0] < b[2] <= image.width and 0 <= b[1] < b[3] <= image.height):
        raise ValueError("영역이 원본 이미지 안에 있어야 합니다.")
    return (math.floor(b[0]), math.floor(b[1]), math.ceil(b[2]), math.ceil(b[3]))


def prepare(image: Image.Image, region: Region) -> tuple[bytes, bool]:
    box = region_box(image, region)
    crop = image.crop((max(0, box[0] - 3), max(0, box[1] - 3),
                       min(image.width, box[2] + 3), min(image.height, box[3] + 3)))
    # 흰 여백만 제거한다. 사진의 그림자·배경을 문서 영역이라고 단정하지 않는다.
    ink = ImageChops.difference(crop, Image.new("RGB", crop.size, "white"))
    tight = ink.convert("L").point(lambda x: 255 if x > 20 else 0).getbbox()
    if tight is None:
        raise ValueError(f"내용이 없는 영역입니다: {region.id}")
    crop = crop.crop(tight)
    scale = min(2.0, 3200 / max(crop.size), math.sqrt(4_000_000 / (crop.width * crop.height)))
    crop = crop.resize((max(1, round(crop.width * scale)), max(1, round(crop.height * scale))),
                       Image.Resampling.LANCZOS)
    output = io.BytesIO()
    ImageOps.expand(crop, border=16, fill="white").save(output, format="PNG")
    return output.getvalue(), scale < 1


def uncovered_content(image: Image.Image, regions: list[Region]) -> int:
    """선택 영역 밖 어두운 픽셀 수. 테두리·그림자도 포함하므로 누락 확정값은 아니다."""
    mask = Image.new("L", image.size, 255)
    draw = ImageDraw.Draw(mask)
    for region in regions:
        l, t, r, b = region_box(image, region)
        draw.rectangle((max(0, l - 3), max(0, t - 3), min(image.width, r + 3),
                        min(image.height, b + 3)), fill=0)
    dark = image.convert("L").point(lambda x: 255 if x < 150 else 0)
    return ImageChops.multiply(dark, mask).histogram()[255]
