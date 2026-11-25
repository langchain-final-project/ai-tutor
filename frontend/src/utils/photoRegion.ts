import type { PercentRegion, PhotoRegion } from "../types/photoRegion"

const clampPercent = (value: number) => Math.min(100, Math.max(0, value))

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
  ) return null

  // 화면 크기와 무관한 비율을 원본 픽셀로 변환합니다.
  // 바깥으로 반올림하되 이미지 경계를 넘지 않게 합니다.
  const left = Math.floor(imageWidth * clampPercent(crop.x) / 100 + 1e-7)
  const top = Math.floor(imageHeight * clampPercent(crop.y) / 100 + 1e-7)
  const right = Math.ceil(imageWidth * clampPercent(crop.x + crop.width) / 100 - 1e-7)
  const bottom = Math.ceil(imageHeight * clampPercent(crop.y + crop.height) / 100 - 1e-7)
  if (right <= left || bottom <= top) return null

  return {
    coordinate_space: "exif_transposed",
    image_size: [imageWidth, imageHeight],
    bbox: [left, top, right, bottom],
  }
}

export function isFullPhotoRegion(region: PhotoRegion | null): boolean {
  return region !== null && region.bbox[0] === 0 && region.bbox[1] === 0 &&
    region.bbox[2] === region.image_size[0] && region.bbox[3] === region.image_size[1]
}

export async function createPhotoRegionPreviewUrl(
  imageUrl: string,
  region: PhotoRegion,
  signal: AbortSignal,
): Promise<string> {
  signal.throwIfAborted()

  const image = new Image()
  image.src = imageUrl
  await image.decode()

  signal.throwIfAborted()

  if (
    image.naturalWidth !== region.image_size[0] ||
    image.naturalHeight !== region.image_size[1]
  ) {
    throw new Error("사진과 선택 범위의 크기가 일치하지 않습니다.")
  }

  const [left, top, right, bottom] = region.bbox

  if (
    !region.bbox.every(Number.isSafeInteger) ||
    left < 0 ||
    top < 0 ||
    right > image.naturalWidth ||
    bottom > image.naturalHeight ||
    right <= left ||
    bottom <= top
  ) {
    throw new Error("선택한 사진 범위가 올바르지 않습니다.")
  }

  const width = right - left
  const height = bottom - top

  // 화면에 보여줄 사본만 축소합니다.
  const ratio = Math.min(1, 1600 / Math.max(width, height))

  const canvas = document.createElement("canvas")
  canvas.width = Math.max(1, Math.round(width * ratio))
  canvas.height = Math.max(1, Math.round(height * ratio))

  const context = canvas.getContext("2d")

  if (!context) {
    throw new Error("선택한 사진을 표시하지 못했습니다.")
  }

  context.drawImage(
    image,
    left,
    top,
    width,
    height,
    0,
    0,
    canvas.width,
    canvas.height,
  )

  const blob = await new Promise<Blob>((resolve, reject) => {
    canvas.toBlob((result) => {
      if (result) resolve(result)
      else reject(new Error("선택한 사진을 만들지 못했습니다."))
    }, "image/png")
  })

  signal.throwIfAborted()

  return URL.createObjectURL(blob)
}