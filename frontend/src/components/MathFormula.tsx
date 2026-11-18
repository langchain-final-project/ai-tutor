import katex from "katex"
import "katex/dist/katex.min.css"

export function MathFormula({ latex }: { latex: string }) {
  const markup = katex.renderToString(latex, {
    throwOnError: false,
    trust: false,
  })

  return (
    <span
      className="review-formula"
      dangerouslySetInnerHTML={{ __html: markup }}
    />
  )
}