import type { PercentRegion, PhotoRegion } from "../types/photoRegion";

const clampPercent = (value: number) => Math.min(100, Math.max(0, value));

export function toPhotoRegion(
  crop: PercentRegion,
  imageWidth: number,
  imageHeight: number,
): PhotoRegion | null {
  if (
    !Number.isSafeInteger(imageWidth) || imageWidth < 1 ||
    !Number.isSafeInteger(imageHeight) || imageHeight < 1 ||
    ![crop.x, crop.y, crop.width, crop.height].every(Number.isFinite) ||
    crop.width <= 0 || crop.height <= 0
  ) return null;

  // 화면 크기와 무관한 비율을 원본 픽셀로 변환합니다.
  // 바깥으로 반올림하되 이미지 경계를 넘지 않게 합니다.
  const left = Math.floor(imageWidth * clampPercent(crop.x) / 100 + 1e-7);
  const top = Math.floor(imageHeight * clampPercent(crop.y) / 100 + 1e-7);
  const right = Math.ceil(imageWidth * clampPercent(crop.x + crop.width) / 100 - 1e-7);
  const bottom = Math.ceil(imageHeight * clampPercent(crop.y + crop.height) / 100 - 1e-7);
  if (right <= left || bottom <= top) return null;

  return {
    coordinate_space: "exif_transposed",
    image_size: [imageWidth, imageHeight],
    bbox: [left, top, right, bottom],
  };
}

export function isFullPhotoRegion(region: PhotoRegion | null): boolean {
  return region !== null && region.bbox[0] === 0 && region.bbox[1] === 0 &&
    region.bbox[2] === region.image_size[0] && region.bbox[3] === region.image_size[1];
}
