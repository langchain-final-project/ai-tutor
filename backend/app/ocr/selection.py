"""EXIF 보정 원본의 선택 영역을 검증하고 모델 입력 사본을 만든다."""

from io import BytesIO

from app.ocr.images import open_image


class InvalidPhotoRegion(ValueError):
    pass


def validate_photo_region(content: bytes, region: dict) -> dict:
    if not isinstance(region, dict) or set(region) != {"coordinate_space", "image_size", "bbox"}:
        raise InvalidPhotoRegion("선택 영역의 형식을 확인해 주세요.")
    if region["coordinate_space"] != "exif_transposed":
        raise InvalidPhotoRegion("선택 영역은 EXIF 방향을 반영한 원본 사진 기준이어야 합니다.")
    size, bbox = region["image_size"], region["bbox"]
    if (
        not isinstance(size, (list, tuple)) or len(size) != 2
        or not isinstance(bbox, (list, tuple)) or len(bbox) != 4
        or any(type(value) is not int for value in [*size, *bbox])
    ):
        raise InvalidPhotoRegion("사진 크기와 선택 영역은 정수 픽셀 좌표로 지정해 주세요.")
    with open_image(content) as image:
        if tuple(size) != image.size:
            raise InvalidPhotoRegion("사진 크기가 선택 당시와 다릅니다. 원본에서 범위를 다시 선택해 주세요.")
    width, height = size
    left, top, right, bottom = bbox
    if not (0 <= left < right <= width and 0 <= top < bottom <= height):
        raise InvalidPhotoRegion("선택 영역이 사진 안에 있고, 너비와 높이가 1픽셀 이상인지 확인해 주세요.")
    return {"coordinate_space": "exif_transposed", "image_size": list(size), "bbox": list(bbox)}


def crop_photo_region(content: bytes, region: dict) -> bytes:
    region = validate_photo_region(content, region)
    if region["bbox"] == [0, 0, *region["image_size"]]:
        return content
    with open_image(content) as image, image.crop(tuple(region["bbox"])) as cropped:
        with BytesIO() as output:
            cropped.save(output, format="PNG")
            return output.getvalue()
