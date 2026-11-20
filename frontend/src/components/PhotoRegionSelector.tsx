import { useEffect, useMemo, useRef, useState } from "react"
import ReactCrop from "react-image-crop"
import type { PercentCrop } from "react-image-crop"
import type { PhotoRegion } from "../types/photoRegion"
import { toPhotoRegion } from "../utils/photoRegion"
import "react-image-crop/dist/ReactCrop.css"

const fullCrop: PercentCrop = { unit: "%", x: 0, y: 0, width: 100, height: 100 }

type PhotoRegionSelectorProps = {
  imageUrl: string
  onChange: (region: PhotoRegion | null) => void
}

export function PhotoRegionSelector({ imageUrl, onChange }: PhotoRegionSelectorProps) {
  const imageRef = useRef<HTMLImageElement>(null)
  // const canvasRef = useRef<HTMLCanvasElement>(null)
  const [crop, setCrop] = useState<PercentCrop>(fullCrop)
  const [imageSize, setImageSize] = useState<[number, number]>([0, 0])
  const [error, setError] = useState("")

  const region = useMemo(
    () => toPhotoRegion(crop, imageSize[0], imageSize[1]),
    [crop, imageSize],
  )

  useEffect(() => {
    onChange(error ? null : region)
  }, [region, error, onChange])

  // useEffect(() => {
  //   if (!region || error) return
  //   const frame = window.requestAnimationFrame(() => {
  //     const image = imageRef.current
  //     const canvas = canvasRef.current
  //     if (!image || !canvas) return
  //     const [left, top, right, bottom] = region.bbox
  //     const width = right - left
  //     const height = bottom - top
  //     // 미리보기만 축소하고, 저장할 좌표는 원본 해상도를 유지합니다.
  //     const ratio = Math.min(1, 1024 / width, 1024 / height)
  //     canvas.width = Math.max(1, Math.round(width * ratio))
  //     canvas.height = Math.max(1, Math.round(height * ratio))
  //     const context = canvas.getContext("2d")
  //     if (!context) {
  //       setError("선택 영역 미리보기를 표시하지 못했습니다.")
  //       return
  //     }
  //     try {
  //       context.drawImage(image, left, top, width, height, 0, 0, canvas.width, canvas.height)
  //     } catch {
  //       setError("선택 영역 미리보기를 표시하지 못했습니다.")
  //     }
  //   })
  //   return () => window.cancelAnimationFrame(frame)
  // }, [region, error])

  return (
    <section className="review-region-selector" aria-label="문제 범위 선택">
      <div className="review-region-heading">
        <h3>문제 범위 선택</h3>
        <button type="button" disabled={!region} onClick={() => setCrop({ ...fullCrop })}>
          전체 사진 사용
        </button>
      </div>
      <p id="review-region-help" className="review-help">
        문제 하나만 선택해 주세요~
      </p>
      <div className="review-region-stage" aria-describedby="review-region-help">
        <ReactCrop
          crop={crop}
          onChange={(_, percentCrop) => setCrop(percentCrop)}
          keepSelection
          minWidth={32}
          minHeight={32}
          disabled={!region || !!error}
          ariaLabels={{
            cropArea: "선택 범위 이동",
            nwDragHandle: "왼쪽 위 모서리 조절",
            nDragHandle: "위쪽 테두리 조절",
            neDragHandle: "오른쪽 위 모서리 조절",
            eDragHandle: "오른쪽 테두리 조절",
            seDragHandle: "오른쪽 아래 모서리 조절",
            sDragHandle: "아래쪽 테두리 조절",
            swDragHandle: "왼쪽 아래 모서리 조절",
            wDragHandle: "왼쪽 테두리 조절",
          }}
        >
          <img
            ref={imageRef}
            src={imageUrl}
            alt="범위를 선택할 원본 문제 사진"
            className="review-region-image"
            draggable={false}
            onLoad={(event) => {
              const image = event.currentTarget
              setImageSize([image.naturalWidth, image.naturalHeight])
            }}
            onError={() => setError("사진을 표시하지 못했습니다. 사진을 다시 선택해 주세요.")}
          />
        </ReactCrop>
      </div>
      {error && <p className="review-error" role="alert">{error}</p>}
      {/* {region && !error && (
        <div className="review-region-preview" data-photo-region={JSON.stringify(region)}>
          <h3>선택 영역 미리보기</h3>
          <p className="review-help">
            선택 크기: {region.bbox[2] - region.bbox[0]} × {region.bbox[3] - region.bbox[1]} 픽셀
          </p>
          <canvas ref={canvasRef} role="img" aria-label="선택한 문제 범위 미리보기" />
        </div>
      )} */}
    </section>
  )
}
