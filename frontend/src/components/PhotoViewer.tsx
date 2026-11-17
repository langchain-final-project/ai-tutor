import { useLayoutEffect, useRef, useState } from "react";
import {
  TransformComponent,
  TransformWrapper,
} from "react-zoom-pan-pinch";

type PhotoViewerProps = {
  imageUrl: string;
  onError: () => void;
};

const FRAME_HEIGHT = 280;
const MAX_SCALE = 6;

export function PhotoViewer({
  imageUrl,
  onError,
}: PhotoViewerProps) {
  const frameRef = useRef<HTMLDivElement>(null);

  const [frameWidth, setFrameWidth] = useState(0);
  const [naturalSize, setNaturalSize] = useState({
    width: 0,
    height: 0,
  });
  const [scale, setScale] = useState(1);

  useLayoutEffect(() => {
    const frame = frameRef.current;
    if (!frame) return;

    const observer = new ResizeObserver(([entry]) => {
      setFrameWidth(entry.contentRect.width);
    });

    observer.observe(frame);

    return () => observer.disconnect();
  }, []);

  const ready =
    frameWidth > 0 &&
    naturalSize.width > 0 &&
    naturalSize.height > 0;

  // 사진 전체가 처음에는 영역 안에 들어오도록 크기를 계산합니다.
  const fitRatio = ready
    ? Math.min(
        frameWidth / naturalSize.width,
        FRAME_HEIGHT / naturalSize.height,
      )
    : 1;

  const imageWidth = ready ? naturalSize.width * fitRatio : 1;
  const imageHeight = ready ? naturalSize.height * fitRatio : 1;
  const zoomed = scale > 1.01;

  return (
    <div className="review-photo-viewer">
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
                    const image = event.currentTarget;

                    setNaturalSize({
                      width: image.naturalWidth,
                      height: image.naturalHeight,
                    });
                  }}
                  onError={onError}
                />
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
  );
}