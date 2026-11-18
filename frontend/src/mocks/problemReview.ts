import type { ProblemReview, ReviewBlock } from "../types/problemReview"

// 실제 OCR 결과가 아닌, 화면 개발용 예시 문제입니다.
const formulaExample: ReviewBlock[] = [
  {
    id: "paragraph-1",
    kind: "paragraph",
    parts: [
      {
        id: "text-1",
        kind: "text",
        text: "다음 방정식 ",
      },
      {
        id: "math-1",
        kind: "math",
        latex: "x^{2}-6x=0",
      },
      {
        id: "text-2",
        kind: "text",
        text: "을 풀어라.",
      },
    ],
  },
]

export function createFormulaExample(): ProblemReview {
  return {
    id: "formula-example",
    imageUrl: null,
    original: structuredClone(formulaExample),
    draft: structuredClone(formulaExample),
  }
}