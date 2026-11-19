export type OcrStreamEvent = {
  id?: string
  event: string
  data: string
}

// TCP 청크 경계와 SSE 이벤트 경계는 다르므로 줄 단위로 조립합니다.
export async function readEventStream(
  body: ReadableStream<Uint8Array>,
  onEvent: (event: OcrStreamEvent) => boolean | void,
  onActivity: () => void = () => {},
): Promise<void> {
  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buffer = ""
  let name = "message"
  let id: string | undefined
  let data: string[] = []
  let stopped = false

  function line(value: string) {
    if (!value) {
      if (data.length) {
        stopped = onEvent({ id, event: name, data: data.join("\n") }) === false
      }
      name = "message"
      id = undefined
      data = []
      return
    }
    if (value.startsWith(":")) return
    const separator = value.indexOf(":")
    const key = separator < 0 ? value : value.slice(0, separator)
    let field = separator < 0 ? "" : value.slice(separator + 1)
    if (field.startsWith(" ")) field = field.slice(1)
    if (key === "event") name = field
    if (key === "id" && !field.includes("\0")) id = field
    if (key === "data") data.push(field)
  }

  try {
    while (!stopped) {
      const chunk = await reader.read()
      if (!chunk.done) onActivity()
      buffer += decoder.decode(chunk.value, { stream: !chunk.done })
      if (buffer.length > 1_000_000) throw new Error("진행 이벤트가 너무 큽니다.")

      while (!stopped) {
        const end = buffer.search(/[\r\n]/)
        if (end < 0) break
        if (buffer[end] === "\r" && end === buffer.length - 1 && !chunk.done) break
        const size = buffer[end] === "\r" && buffer[end + 1] === "\n" ? 2 : 1
        const value = buffer.slice(0, end)
        buffer = buffer.slice(end + size)
        line(value)
      }
      if (chunk.done) break
    }
    // 빈 줄로 끝나지 않은 마지막 이벤트는 재연결 때 다시 받습니다.
  } finally {
    await reader.cancel().catch(() => {})
    reader.releaseLock()
  }
}

export class OcrStreamHttpError extends Error {
  constructor(public status: number) {
    super(`진행 상황 연결에 실패했습니다. (${status})`)
  }
}

export async function connectOcrStream(
  jobId: string,
  afterId: string,
  signal: AbortSignal,
  onEvent: (event: OcrStreamEvent) => boolean | void,
  onConnected: () => void,
  idleTimeoutMs = 30_000,
): Promise<void> {
  signal.throwIfAborted()
  const connection = new AbortController()
  const abort = () => connection.abort(signal.reason)
  signal.addEventListener("abort", abort, { once: true })
  let idle: ReturnType<typeof setTimeout> | undefined
  const activity = () => {
    clearTimeout(idle)
    idle = setTimeout(() => connection.abort(), idleTimeoutMs)
  }

  try {
    activity()
    const response = await fetch(`/ocr-api/ocr-jobs/${encodeURIComponent(jobId)}/events`, {
      headers: { Accept: "text/event-stream", "Last-Event-ID": afterId },
      signal: connection.signal,
      cache: "no-store",
    })
    if (!response.ok) throw new OcrStreamHttpError(response.status)
    if (!response.body || !response.headers.get("content-type")?.includes("text/event-stream")) {
      throw new Error("진행 이벤트 형식을 확인하지 못했습니다.")
    }
    onConnected()
    await readEventStream(response.body, onEvent, activity)
  } finally {
    clearTimeout(idle)
    signal.removeEventListener("abort", abort)
    connection.abort()
  }
}
