export type OcrProgress = {
  stage: string
  attempt: number
  text: string
  connection: "live" | "reconnecting" | "polling"
}

export function initialOcrProgress(): OcrProgress {
  return { stage: "queued", attempt: 1, text: "", connection: "live" }
}

export function updateOcrProgress(
  previous: OcrProgress,
  event: string,
  data: Record<string, unknown>,
): OcrProgress {
  let next = previous
  if (Number.isInteger(data.attempt) && Number(data.attempt) > previous.attempt) {
    next = { ...previous, attempt: Number(data.attempt), text: "" }
  }
  if (event === "queued" || event === "running") {
    return { ...next, stage: event }
  }
  if (event === "stage" && typeof data.stage === "string") {
    return { ...next, stage: data.stage }
  }
  if (event === "attempt_complete") return { ...next, stage: "checking" }
  if (event === "completed") return { ...next, stage: "finalizing" }

  // 재시도나 보조 영역의 결과를 한 문장에 이어 붙이지 않습니다.
  // 전체 문제의 임시 본문만 표시하고 결합 결과는 완료 후 조회합니다.
  const wholeProblem = data.region_id === undefined || data.region_id === "problem"
  if (!wholeProblem) return next
  if (event === "region_start") return { ...next, text: "" }
  if (event === "delta" && typeof data.text === "string") {
    return { ...next, text: (next.text + data.text).slice(0, 24_000) }
  }
  if (event === "region_complete" && typeof data.text === "string") {
    return { ...next, text: data.text.slice(0, 24_000) }
  }
  return next
}

const stageLabels: Record<string, string> = {
  queued: "문제를 읽을 순서를 기다리고 있어요.",
  running: "문제 인식을 시작하고 있어요.",
  orientation: "사진 방향을 확인하고 있어요.",
  layout: "글·수식·그림의 위치를 확인하고 있어요.",
  qwen: "문제 내용을 읽고 있어요.",
  paddle: "문제 내용을 읽고 있어요.",
  formula: "수식을 확인하고 있어요.",
  assembly: "본문과 수식을 정리하고 있어요.",
  retry: "읽기 어려운 부분을 다시 읽고 있어요.",
  checking: "인식한 내용을 확인하고 있어요.",
  verification: "원본 사진과 인식 내용을 비교하고 있어요.",
  conditions: "그림에 적힌 조건을 확인하고 있어요.",
  finalizing: "완료된 인식 결과를 불러오고 있어요.",
}

export function ocrProgressMessage(progress: OcrProgress): string {
  if (progress.connection === "reconnecting") return "진행 상황을 다시 연결하고 있어요."
  if (progress.connection === "polling") return "진행 상태를 확인하고 있어요. 완료되면 결과를 보여드릴게요."
  return stageLabels[progress.stage] ?? "문제 인식을 진행하고 있어요."
}

// 아직 닫히지 않은 수식 구분 기호는 다음 토큰을 기다립니다.
export function completeMathPrefix(text: string): string {
  let closing = ""
  let start = -1
  let braceDepth = 0
  let braceStart = -1
  for (let i = 0; i < text.length; i++) {
    if (closing) {
      if (text.startsWith(closing, i)) {
        i += closing.length - 1
        closing = ""
        start = -1
      } else if (text[i] === "\\") {
        i++
      }
      continue
    }
    if (text[i] === "\\") {
      if (i === text.length - 1) return text.slice(0, i)
      if (text[i + 1] === "(" || text[i + 1] === "[") {
        start = i
        closing = text[i + 1] === "(" ? "\\)" : "\\]"
      }
      i++
    } else if (text[i] === "{") {
      if (braceDepth++ === 0) braceStart = i
    } else if (text[i] === "}") {
      braceDepth = Math.max(0, braceDepth - 1)
    } else if (text[i] === "$") {
      start = i
      closing = text[i + 1] === "$" ? "$$" : "$"
      i += closing.length - 1
    }
  }
  if (closing) return text.slice(0, start)
  if (braceDepth > 0) {
    const markers = [...text.slice(0, braceStart).matchAll(/\\[A-Za-z]+|[A-Za-z0-9]+\s*[_^]/g)]
    const last = markers.at(-1)
    return text.slice(0, last?.index ?? braceStart)
  }
  return text
}
