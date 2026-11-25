import { MathFormula } from "./MathFormula"
import type { ConditionExtraction } from "../types/problemCondition"

type ProblemConditionsProps = {
  extraction?: ConditionExtraction | null
}

export function ProblemConditions({
  extraction,
}: ProblemConditionsProps) {
  if (!extraction || extraction.status === "not_run") {
    return null
  }

  return (
    <section
      className="review-section"
      aria-labelledby="review-conditions-title"
    >
      <div className="review-section-heading">
        <h2 id="review-conditions-title">
          문제에서 읽은 조건
        </h2>
      </div>

      {extraction.status === "failed" ? (
        <p className="review-help" role="status">
          조건을 읽지 못했어요. 원본 사진과 본문을 확인해 주세요.
        </p>
      ) : extraction.conditions.length === 0 ? (
        <p className="review-help">
          별도로 읽어낸 조건이 없어요.
          원본에 필요한 조건이 있는지 확인해 주세요.
        </p>
      ) : (
        <ul className="review-condition-list">
          {extraction.conditions.map((condition) => (
            <li
              key={condition.id}
              className="review-condition-card"
            >
              <p className="review-condition-label">
                {condition.label}
              </p>

              <div className="review-condition-value">
                {condition.value_format === "latex" ? (
                  <MathFormula latex={condition.value} />
                ) : (
                  condition.value
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}