import { useRef, useState } from "react";
import type { ChangeEvent } from "react";
import { PhotoViewer } from "./PhotoViewer";

const acceptedTypes = new Set([
  "image/jpeg",
  "image/png",
  "image/webp",
]);

type PhotoPickerProps = {
  onSelect: (file: File) => boolean;
  imageUrl: string | null;
  disabled?: boolean;
};

export function PhotoPicker({
  onSelect,
  imageUrl,
  disabled = false,
}: PhotoPickerProps) {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const cameraInputRef = useRef<HTMLInputElement>(null);

  const [error, setError] = useState("");

  function selectPhoto(event: ChangeEvent<HTMLInputElement>) {
    const nextFile = event.currentTarget.files?.[0];

    // 같은 사진을 다시 골라도 change 이벤트가 발생하게 합니다.
    event.currentTarget.value = "";

    // 사진 선택이나 촬영을 취소하면 기존 사진을 유지합니다.
    if (!nextFile || disabled) return;

    if (nextFile.size > 30 * 1024 * 1024) {
      setError("사진은 30MB 이하로 선택해 주세요.");
      return;
    }

    if (nextFile.type && !acceptedTypes.has(nextFile.type)) {
      setError("JPG, PNG, WebP 사진을 선택해 주세요.");
      return;
    }

    if (!onSelect(nextFile)) return;

    setError("");
  }

  return (
    <div className="review-photo-picker">
      {imageUrl ? (
      <PhotoViewer
        key={imageUrl}
        imageUrl={imageUrl}
        onError={() => {
          setError(
            "사진을 표시할 수 없습니다. 다른 사진을 선택해 주세요.",
          );
        }}
      />
      ) : (
        <div className="review-photo-placeholder">
          <span>문제 사진을 올려주세요.</span>
          <p>직접 촬영하거나 저장된 사진을 선택할 수 있습니다.</p>
        </div>
      )}

      <div className="review-photo-actions">
        <button
          type="button"
          disabled={disabled}
          onClick={() => fileInputRef.current?.click()}
        >
          {imageUrl ? "사진 다시 선택" : "사진 선택"}
        </button>

        <button
          type="button"
          disabled={disabled}
          onClick={() => cameraInputRef.current?.click()}
        >
          {imageUrl ? "재촬영" : "사진 촬영"}
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

      <p className="review-help">
        사진을 선택하면 업로드 후 문제 내용을 인식합니다.
      </p>
    </div>
  );
}
