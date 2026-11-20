import { createReviewProblem } from "../api/problemOcr"
import { displayLatexSpacing } from "../utils/reviewLatex"
import { completeMathPrefix, ocrProgressMessage } from "../utils/ocrProgress"
import type { OcrProgress } from "../utils/ocrProgress"
import { MathFormula } from "./MathFormula"

export function OcrLivePreview({ progress }: { progress: OcrProgress | null }) {
  const text = completeMathPrefix(progress?.text ?? "")
  const preview = createReviewProblem({
    id: 0,
    review_status: "awaiting_input",
    latest_input: { content_text: text, version_number: 1, confirmed_at: null },
  })

  return (
    <div className="review-live-preview" aria-label="실시간 인식 내용">
      <div className="review-live-heading" role="status" aria-live="polite">
        <span className="review-live-spinner" aria-hidden="true" />
        <span>{progress ? ocrProgressMessage(progress) : "문제 인식을 준비하고 있어요."}</span>
      </div>
      <p className="review-live-notice">
        인식 중인 임시 내용이에요. 내용이 바뀔 수 있으며 완료 후 수정할 수 있어요.
      </p>
      {progress && progress.attempt > 1 && (
        <p className="review-help">다시 읽은 내용을 확인하고 있어요.</p>
      )}
      {text.trim() ? (
        <div className="review-live-text" aria-live="off">
          {preview.draft.map((block) => block.kind === "paragraph" && (
            <p key={block.id}>
              {block.parts.map((part) => part.kind === "math" ? (
                <MathFormula key={part.id} latex={part.latex} pending />
              ) : (
                <span key={part.id}>{displayLatexSpacing(part.text)}</span>
              ))}
            </p>
          ))}
        </div>
      ) : (
        <p className="review-help">글자가 읽히는 대로 여기에 표시돼요.</p>
      )}
      {!!progress?.text && text.length < progress.text.length && (
        <span className="review-help">수식을 읽고 있어요…</span>
      )}
    </div>
  )
}
