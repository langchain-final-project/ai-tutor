import { useEffect, useRef, useState } from "react"
import { chooseUnitType, matchUnitType } from "../api/problemOcr"
import type { Submission, UnitTypeMatch } from "../api/problemOcr"
import { MathFormula } from "./MathFormula"


function ReferenceText({ text }: { text: string }) {
  const pattern = /\\\(([\s\S]*?)\\\)|\\\[([\s\S]*?)\\\]|\$\$([\s\S]*?)\$\$|(?<!\\)\$([^$\n]+)(?<!\\)\$/g
  const parts = []
  let cursor = 0
  for (const match of text.matchAll(pattern)) {
    parts.push(text.slice(cursor, match.index))
    parts.push(<MathFormula key={match.index} latex={match[1] ?? match[2] ?? match[3] ?? match[4]} />)
    cursor = match.index! + match[0].length
  }
  parts.push(text.slice(cursor))
  return <>{parts}</>
}


export function ProblemTypeMatch({ submission, hasEdits, disabled }: {
  submission: Submission; hasEdits: boolean; disabled: boolean
}) {
  const [result, setResult] = useState<UnitTypeMatch | null>(hasEdits ? null : submission.type_match ?? null)
  const [pending, setPending] = useState<string | null>(null)
  const [error, setError] = useState("")
  const controllerRef = useRef<AbortController | null>(null)
  const latest = submission.latest_input
  const eligible = !!latest?.confirmed_at && !hasEdits

  useEffect(() => () => {
    controllerRef.current?.abort()
    controllerRef.current = null
  }, [])

  async function classify(code?: string) {
    if (!latest || !eligible || disabled || pending) return
    const controller = new AbortController()
    controllerRef.current = controller
    const timer = window.setTimeout(() => controller.abort(), 30_000)
    setPending(code ?? "search")
    setError("")
    try {
      const next = code
        ? await chooseUnitType(submission.id, latest.version_number, code, controller.signal)
        : await matchUnitType(submission.id, latest.version_number, controller.signal)
      if (controllerRef.current === controller) setResult(next)
    } catch (error) {
      if (controllerRef.current !== controller) return
      setError(controller.signal.aborted ? "유형 검색 시간이 초과됐습니다. 다시 시도해 주세요."
        : error instanceof Error ? error.message : "유형 후보를 불러오지 못했습니다.")
    } finally {
      window.clearTimeout(timer)
      if (controllerRef.current === controller) {
        controllerRef.current = null
        setPending(null)
      }
    }
  }

  return (
    <section className="review-section review-type-match" aria-labelledby="review-type-title" aria-busy={pending !== null}>
      <div className="review-section-heading">
        <h2 id="review-type-title">문제 유형 찾기</h2>
        <p>중1 · 1학기 · 3단원 문자와 식</p>
      </div>
      {!eligible && <p className="review-help">본문·수식·필요한 조건을 확인한 뒤 아래의 ‘문제 내용 확인 완료’를 눌러주세요.</p>}
      <button type="button" className="review-type-search" disabled={!eligible || disabled || pending !== null}
        onClick={() => void classify()}>
        {pending === "search" ? "유형 후보를 찾고 있어요…" : "문자와 식 유형 후보 찾기"}
      </button>
      {error && <p className="review-error" role="alert">{error}</p>}
      {result && eligible && (
        <>
          <p className="review-help">문제은행 {result.bank_question_count.toLocaleString()}문항 · {result.bank_type_count}개 유형에서 비교했어요.</p>
          {result.selected_type && (
            <p className="review-type-selected" role="status">
              선택된 유형: <strong>{result.selected_type.source_topic_name}</strong>
              {result.method === "exact_match" && <span> · 동일 본문 문항 매칭</span>}
            </p>
          )}
          {result.reasons.map((reason, index) => <p className="review-help" key={index}>{reason}</p>)}
          <ol className="review-type-candidates">
            {result.candidates.map((candidate) => {
              const selected = result.selected_type?.source_topic_code === candidate.source_topic_code
              return (
                <li key={candidate.source_topic_code}>
                  <h3>{candidate.source_topic_name}</h3>
                  <p className="review-help">유형 코드 {candidate.source_topic_code}</p>
                  {candidate.reasons.map((reason, index) => <p key={index}>{reason}</p>)}
                  <details>
                    <summary>비슷한 문제은행 문항 보기</summary>
                    {candidate.reference_questions.slice(0, 2).map((question) => (
                      <div className="review-type-reference" key={question.id}>
                        <p><ReferenceText text={question.question_text} /></p>
                      </div>
                    ))}
                  </details>
                  <button type="button" disabled={disabled || pending !== null || selected}
                    onClick={() => void classify(candidate.source_topic_code)}>
                    {pending === candidate.source_topic_code ? "저장 중…" : selected ? "선택됨" : "이 유형으로 선택"}
                  </button>
                </li>
              )
            })}
          </ol>
        </>
      )}
    </section>
  )
}
