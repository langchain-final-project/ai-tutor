import { useLayoutEffect, useRef, useState } from "react"
import {
  TransformComponent,
  TransformWrapper,
} from "react-zoom-pan-pinch"
import type {LayoutKind, ProblemLayout,} from "../types/problemLayout"


type PhotoViewerProps = {
  imageUrl: string
  onError: () => void
  layout?: ProblemLayout | null
}

const FRAME_HEIGHT = 280
const MAX_SCALE = 6

const LAYOUT_COLORS: Record<LayoutKind, string> = {
  group: "#64748b",
  text: "#2563eb",
  formula: "#dc2626",
  table: "#15803d",
  figure: "#9333ea",
  graph: "#c2410c",
  unknown: "#475569",
}


export function PhotoViewer({
  imageUrl,
  onError,
  layout = null
}: PhotoViewerProps) {
  const frameRef = useRef<HTMLDivElement>(null)

  const [frameWidth, setFrameWidth] = useState(0)
  const [naturalSize, setNaturalSize] = useState({
    width: 0,
    height: 0,
  })
  const [scale, setScale] = useState(1)
  const [showLayout, setShowLayout] = useState(true)

  useLayoutEffect(() => {
    const frame = frameRef.current
    if (!frame) return

    const observer = new ResizeObserver(([entry]) => {
      setFrameWidth(entry.contentRect.width)
    })

    observer.observe(frame)

    return () => observer.disconnect()
  }, [])

  const ready =
    frameWidth > 0 &&
    naturalSize.width > 0 &&
    naturalSize.height > 0

  // 사진 전체가 처음에는 영역 안에 들어오도록 크기를 계산합니다.
  const fitRatio = ready
    ? Math.min(
        frameWidth / naturalSize.width,
        FRAME_HEIGHT / naturalSize.height,
      )
    : 1

  const imageWidth = ready ? naturalSize.width * fitRatio : 1
  const imageHeight = ready ? naturalSize.height * fitRatio : 1
  const zoomed = scale > 1.01

  // 표시 중인 사진과 영역 좌표의 기준 이미지가 같은지 확인합니다.
  const layoutMatches =
    ready &&
    layout?.coordinate_space === "ocr_image" &&
    layout.image_size[0] === naturalSize.width &&
    layout.image_size[1] === naturalSize.height

  return (
    <div className="review-photo-viewer">
      {layoutMatches && (
        <div>
          <label>
            <input
              type="checkbox"
              checked={showLayout}
              onChange={(event) => setShowLayout(event.currentTarget.checked)}
            />
            {" "}검출 영역 표시
          </label>

          {showLayout && (
            <p className="review-help">
              파랑: 글자 · 빨강: 수식 · 초록: 표 · 보라: 그림 ·
              주황: 그래프 · 회색 점선: 묶음
            </p>
          )}
        </div>
      )}
      <div ref={frameRef} className="review-photo-frame">
        <TransformWrapper
          key={`${imageWidth}:${imageHeight}`}
          initialScale={1}
          initialPositionX={(frameWidth - imageWidth) / 2}
          initialPositionY={(FRAME_HEIGHT - imageHeight) / 2}
          minScale={1}
          maxScale={MAX_SCALE}
          centerZoomedOut
          limitToBounds
          wheel={{ disabled: true }}
          trackPadPanning={{ disabled: !zoomed }}
          panning={{
            disabled: !zoomed,
            velocityDisabled: true,
          }}
          pinch={{ disabled: false }}
          doubleClick={{
            mode: "toggle",
            step: 1,
            animationTime: 150,
          }}
          zoomAnimation={{ disabled: true }}
          velocityAnimation={{ disabled: true }}
          onInit={() => setScale(1)}
          onTransform={(_, state) => setScale(state.scale)}
        >
          {({ zoomIn, zoomOut, resetTransform }) => (
            <>
              <TransformComponent
                wrapperStyle={{
                  width: "100%",
                  height: FRAME_HEIGHT,
                  touchAction: zoomed ? "none" : "pan-y",
                  cursor: zoomed ? "grab" : "default",
                }}
                contentStyle={{
                  width: imageWidth,
                  height: imageHeight,
                  position: "relative",
                }}
              >
                <img
                  src={imageUrl}
                  alt="선택한 문제 사진"
                  className="review-photo-image"
                  draggable={false}
                  style={{
                    width: imageWidth,
                    height: imageHeight,
                    visibility: ready ? "visible" : "hidden",
                  }}
                  onLoad={(event) => {
                    const image = event.currentTarget

                    setNaturalSize({
                      width: image.naturalWidth,
                      height: image.naturalHeight,
                    })
                  }}
                  onError={onError}
                />
                {showLayout && layoutMatches && layout && (
                  <svg
                    viewBox={`0 0 ${layout.image_size[0]} ${layout.image_size[1]}`}
                    aria-hidden="true"
                    style={{
                      position: "absolute",
                      inset: 0,
                      width: "100%",
                      height: "100%",
                      pointerEvents: "none",
                    }}
                  >
                    {layout.regions
                      .filter((region) => region.id !== "image-root")
                      .map((region) => {
                        const [left, top, right, bottom] = region.bbox

                        return (
                          <rect
                            key={region.id}
                            x={left}
                            y={top}
                            width={right - left}
                            height={bottom - top}
                            fill="none"
                            stroke={LAYOUT_COLORS[region.kind]}
                            strokeWidth={2}
                            strokeDasharray={
                              region.kind === "group" ? "6 4" : undefined
                            }
                            vectorEffect="non-scaling-stroke"
                          />
                        )
                      })}
                  </svg>
                )}
              </TransformComponent>

              <div
                className="review-photo-controls"
                role="group"
                aria-label="사진 확대와 축소"
              >
                <button
                  type="button"
                  aria-label="사진 축소"
                  disabled={scale <= 1.01}
                  onClick={() => void zoomOut(0.3, 150)}
                >
                  −
                </button>

                <output aria-label="사진 배율">
                  {Math.round(scale * 100)}%
                </output>

                <button
                  type="button"
                  aria-label="사진 확대"
                  disabled={scale >= MAX_SCALE - 0.01}
                  onClick={() => void zoomIn(0.3, 150)}
                >
                  ＋
                </button>

                <button
                  type="button"
                  onClick={() => void resetTransform(150)}
                >
                  처음 크기
                </button>
              </div>
            </>
          )}
        </TransformWrapper>
      </div>

      {/* <p className="review-help review-photo-touch-help">
        두 손가락으로 확대·축소하고, 확대된 사진을 끌어서 이동하세요.
        두 번 누르면 확대하거나 처음 크기로 돌아갑니다.
      </p>

      <p className="review-help review-photo-mouse-help">
        ＋/−로 확대·축소하고, 확대된 사진을 드래그하거나 휠로 이동하세요.
      </p> */}
    </div>
  )
}