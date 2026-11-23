import { useRef, useState } from "react"
import type { ChangeEvent, DragEvent, ReactNode } from "react"
import { PhotoViewer } from "./PhotoViewer"
import type { ProblemLayout } from "../types/problemLayout"

const acceptedTypes = new Set([
  "image/jpeg",
  "image/png",
  "image/webp",
])

type PhotoPickerProps = {
  onSelect: (file: File) => boolean
  imageUrl: string | null
  disabled?: boolean
  photoContent?: ReactNode
  layout?: ProblemLayout | null
  photoNotice?: ReactNode
  retakeRequired?: boolean
}

export function PhotoPicker({
  onSelect,
  imageUrl,
  disabled = false,
  photoContent,
  layout = null,
  photoNotice,
  retakeRequired = false,
}: PhotoPickerProps) {
  const fileInputRef = useRef<HTMLInputElement>(null)
  const cameraInputRef = useRef<HTMLInputElement>(null)
  const dragDepthRef = useRef(0)

  const [error, setError] = useState("")
  const [dragging, setDragging] = useState(false)

  function acceptPhoto(nextFile: File | undefined) {
    if (!nextFile || disabled) return

    if (nextFile.size > 30 * 1024 * 1024) {
      setError("사진은 30MB 이하로 선택해 주세요.")
      return
    }

    if (nextFile.type && !acceptedTypes.has(nextFile.type)) {
      setError("JPG, PNG, WebP 사진을 선택해 주세요.")
      return
    }

    if (!onSelect(nextFile)) return

    setError("")
  }

  function selectPhoto(event: ChangeEvent<HTMLInputElement>) {
    const nextFile = event.currentTarget.files?.[0]
    // 같은 사진을 다시 골라도 change 이벤트가 발생하게 합니다.
    event.currentTarget.value = ""
    acceptPhoto(nextFile)
  }

  function enterDrag(event: DragEvent<HTMLDivElement>) {
    if (!event.dataTransfer.types.includes("Files")) return
    event.preventDefault()
    dragDepthRef.current += 1
    if (!disabled) setDragging(true)
  }

  function leaveDrag() {
    dragDepthRef.current = Math.max(0, dragDepthRef.current - 1)
    if (!dragDepthRef.current) setDragging(false)
  }

  function dropPhoto(event: DragEvent<HTMLDivElement>) {
    if (!event.dataTransfer.types.includes("Files")) return
    event.preventDefault()
    dragDepthRef.current = 0
    setDragging(false)
    if (disabled) return
    if (event.dataTransfer.files.length !== 1) {
      setError("문제 사진을 한 장씩 선택해 주세요.")
      return
    }
    acceptPhoto(event.dataTransfer.files[0])
  }

  return (
    <div
      className={`review-photo-picker${dragging && !disabled ? " is-dragging" : ""}`}
      onDragEnter={enterDrag}
      onDragLeave={leaveDrag}
      onDragOver={(event) => {
        if (!event.dataTransfer.types.includes("Files")) return
        event.preventDefault()
        event.dataTransfer.dropEffect = disabled ? "none" : "copy"
      }}
      onDrop={dropPhoto}
    >
      {dragging && !disabled && <div className="review-drop-hint" aria-hidden="true">사진을 놓아 선택하세요</div>}
      {imageUrl ? (
      photoContent ?? <PhotoViewer
        key={imageUrl}
        imageUrl={imageUrl}
        layout={layout}
        onError={() => {
          setError(
            "사진을 표시할 수 없습니다. 다른 사진을 선택해 주세요.",
          )
        }}
      />
      ) : (
        <div className="review-photo-placeholder">
          <span>문제 사진을 올려주세요.</span>
          <p>직접 촬영하거나 저장된 사진을 선택할 수 있습니다.</p>
          <p className="review-photo-drop-help">PC에서는 사진을 이곳에 끌어놓아도 돼요.</p>
          <p>JPG·PNG·WebP · 최대 30MB</p>
        </div>
      )}

      {photoNotice}

      <div className="review-photo-actions">
        <button
          type="button"
          disabled={disabled}
          onClick={() => fileInputRef.current?.click()}
        >
          {retakeRequired ? "다른 사진 올리기" : imageUrl ? "사진 다시 선택" : "사진 선택"}
        </button>

        <button
          type="button"
          disabled={disabled}
          className={retakeRequired ? "review-retake-primary" : undefined}
          onClick={() => cameraInputRef.current?.click()}
        >
          {retakeRequired ? "사진 다시 촬영" : imageUrl ? "재촬영" : "사진 촬영"}
        </button>
      </div>

      <input
        ref={fileInputRef}
        type="file"
        disabled={disabled}
        accept="image/jpeg,image/png,image/webp"
        onChange={selectPhoto}
        hidden
      />

      <input
        ref={cameraInputRef}
        type="file"
        disabled={disabled}
        accept="image/jpeg,image/png,image/webp"
        capture="environment"
        onChange={selectPhoto}
        hidden
      />

      {error && (
        <p className="review-error" role="alert">
          {error}
        </p>
      )}
    </div>
  )
}
