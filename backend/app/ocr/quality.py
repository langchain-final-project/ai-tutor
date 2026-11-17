"""원본을 변경하지 않고 사진 품질 지표를 측정한다."""

import numpy as np
from PIL import Image
from app.ocr.images import open_image


def inspect_photo_quality(content: bytes) -> dict:
    # 기존 함수로 EXIF 방향과 투명 배경을 처리합니다.
    with open_image(content) as image:
        width, height = image.size
        gray = image.convert("L")

    try:
        # 큰 사진은 분석용 사본만 줄입니다.
        # 작은 사진을 확대하지는 않습니다.
        gray.thumbnail(
            (1280, 1280),
            Image.Resampling.LANCZOS,
        )

        pixels = np.asarray(gray, dtype=np.float32)

        sharpness = None

        if min(pixels.shape) >= 3:
            # 주변 픽셀과의 밝기 차이를 측정합니다.
            laplacian = (
                pixels[:-2, 1:-1]
                + pixels[2:, 1:-1]
                + pixels[1:-1, :-2]
                + pixels[1:-1, 2:]
                - 4 * pixels[1:-1, 1:-1]
            )

            sharpness = round(
                float(laplacian.var()),
                3,
            )

        return {
            "image_size": [width, height],
            "analysis_size": list(gray.size),
            "brightness_mean": round(
                float(pixels.mean()), 2
            ),
            "dark_pixel_ratio": round(
                float((pixels <= 20).mean()), 4
            ),
            "bright_pixel_ratio": round(
                float((pixels >= 245).mean()), 4
            ),
            "laplacian_variance": sharpness,
        }

    finally:
        gray.close()

def evaluate_photo_quality(metrics: dict) -> dict:
    """초기 실험용 기준으로 품질 경고를 만든다."""
    width, height = metrics["image_size"]
    analysis_width, analysis_height = metrics["analysis_size"]

    brightness = metrics["brightness_mean"]
    bright_ratio = metrics["bright_pixel_ratio"]
    sharpness = metrics["laplacian_variance"]

    warnings = []

    # 아래 숫자는 실제 촬영 평가 후 조정할 임시 기준입니다.
    low_resolution = min(width, height) < 320
    too_dark = brightness < 60
    too_bright = brightness > 245 and bright_ratio > 0.98

    if low_resolution:
        warnings.append({
            "code": "low_resolution",
            "message": (
                "사진 크기가 작아 글씨를 읽기 어려울 수 있어요. "
                "문제가 크게 나오도록 다시 촬영해 주세요."
            ),
        })

    if too_dark:
        warnings.append({
            "code": "possibly_dark",
            "message": (
                "사진이 어두워 글씨를 읽기 어려울 수 있어요. "
                "밝은 곳에서 다시 촬영해 주세요."
            ),
        })

    if too_bright:
        warnings.append({
            "code": "possibly_overexposed",
            "message": (
                "사진이 지나치게 밝아 글씨가 지워졌을 수 있어요. "
                "빛 반사를 피해 다시 촬영해 주세요."
            ),
        })

    # 작거나 밝기 문제가 있는 사진에서는
    # 낮은 선명도를 곧바로 흔들림으로 해석하지 않습니다.
    can_check_blur = (
        not low_resolution
        and not too_dark
        and not too_bright
        and min(analysis_width, analysis_height) >= 640
        and sharpness is not None
    )

    if can_check_blur and sharpness < 5:
        warnings.append({
            "code": "possibly_blurred",
            "message": (
                "사진이 흐려 글씨를 읽기 어려울 수 있어요. "
                "초점을 맞추고 다시 촬영해 주세요."
            ),
        })

    return {
        "policy_version": "experimental-v1",
        "status": "warning" if warnings else "no_warning",
        "warnings": warnings,
    }