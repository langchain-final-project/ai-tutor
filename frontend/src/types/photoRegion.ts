// EXIF 방향을 반영한 업로드 사진 기준입니다. OCR의 추가 회전 전 좌표입니다.
// bbox는 [왼쪽, 위, 오른쪽, 아래]이며 오른쪽·아래 경계는 포함하지 않습니다.
export type PhotoRegion = {
  coordinate_space: "exif_transposed";
  image_size: [number, number];
  bbox: [number, number, number, number];
};

export type PercentRegion = {
  x: number;
  y: number;
  width: number;
  height: number;
};
