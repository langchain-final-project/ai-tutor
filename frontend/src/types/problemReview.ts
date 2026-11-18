// 본문과 수식을 문장 안에서도 구분할 수 있게 합니다.
export type ContentPart =
  | {
      id: string
      kind: "text"
      text: string
    }
  | {
      id: string
      kind: "math"
      latex: string
      // 수정하지 않은 수식은 OCR의 구분 기호와 공백까지 보존합니다.
      source?: { latex: string; raw: string }
    }

// 도형에서 읽은 조건입니다.
export type FigureCondition = {
  id: string
  kind: "label" | "length" | "angle" | "parallel" | "perpendicular"
  target: string
  value: string
  reviewStatus: "needs_review" | "confirmed"
}

export type ReviewBlock =
  | {
      id: string
      kind: "paragraph"
      parts: ContentPart[]
    }
  | {
      id: string
      kind: "figure"

      // 원본 사진에서 도형이 있는 영역입니다.
      // 영역을 아직 모르면 null로 둡니다.
      region: {
        x: number
        y: number
        width: number
        height: number
      } | null

      conditions: FigureCondition[]
    }

export type ProblemReview = {
  id: string
  imageUrl: string | null

  // 처음 인식한 내용: 편집 화면에서 직접 변경하지 않습니다.
  original: ReviewBlock[]

  // 사용자가 현재 수정하고 있는 내용입니다.
  draft: ReviewBlock[]
}
