import { useEffect, useState } from "react"
import { createReviewProblem } from "../api/problemOcr"
import { displayLatexSpacing } from "../utils/reviewLatex"
import { completeMathPrefix, ocrProgressMessage } from "../utils/ocrProgress"
import type { OcrProgress } from "../utils/ocrProgress"
import { MathFormula } from "./MathFormula"

const WAITING_NOTICE_DELAY_MS = 30_000

export function OcrLivePreview({ progress }: { progress: OcrProgress | null }) {
  const [waiting, setWaiting] = useState(false)

  useEffect(() => {
    setWaiting(false)
    if (!progress) return

    // 연결 유지 신호 대신 실제 단계·시도·본문의 변화를 기준으로 기다립니다.
    const timer = window.setTimeout(() => setWaiting(true), WAITING_NOTICE_DELAY_MS)
    return () => window.clearTimeout(timer)
  }, [progress?.stage, progress?.attempt, progress?.text])

  const showWaitingNotice = waiting && progress?.connection === "live"
  const waitingForFirstText =
    (progress?.stage === "qwen" || progress?.stage === "paddle") &&
    !progress.text.trim()
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
      {showWaitingNotice && (
        <div className="review-live-notice" role="status">
          <strong>
            {waitingForFirstText
              ? "첫 인식 결과를 기다리고 있어요."
              : "현재 단계가 예상보다 오래 걸리고 있어요."}
          </strong>
          <p className="review-live-notice">
            {waitingForFirstText
              ? "아직 글자를 전달받지 못했어요. 사진을 처리하는 데 시간이 걸릴 수 있어요."
              : "새 진행 내용을 기다리고 있어요. 이것만으로 인식 실패를 의미하지는 않아요."}
          </p>
        </div>
      )}
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
