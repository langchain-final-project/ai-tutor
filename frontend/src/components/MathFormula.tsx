import katex from "katex"
import "katex/dist/katex.min.css"

export function MathFormula({ latex, pending = false }: { latex: string; pending?: boolean }) {
  let markup: string

  try {
    markup = katex.renderToString(latex, {
      throwOnError: true,
      trust: false,
    })
  } catch {
    return (
      <span
        className={pending ? "review-stream-math-pending" : "review-unreadable"}
        title={pending ? "수식을 읽고 있어요" : "수식을 눌러 원본과 비교해 수정해 주세요"}
      >
        {pending ? "…" : "수식 확인 필요"}
      </span>
    )
  }

  return (
    <span
      className="review-formula"
      dangerouslySetInnerHTML={{ __html: markup }}
    />
  )
}
