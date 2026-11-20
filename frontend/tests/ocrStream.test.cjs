const { test } = require("node:test")
const assert = require("node:assert/strict")
const fs = require("node:fs")
const path = require("node:path")
const ts = require("typescript")

// 테스트에서는 실제 TypeScript 모듈을 메모리에서 컴파일합니다.
const modules = new Map()
function load(relative) {
  const file = path.resolve(__dirname, "../src", relative)
  if (modules.has(file)) return modules.get(file)
  const result = { exports: {} }
  modules.set(file, result.exports)
  const code = ts.transpileModule(fs.readFileSync(file, "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText
  const localRequire = (name) => name.startsWith(".")
    ? load(path.relative(path.resolve(__dirname, "../src"), path.resolve(path.dirname(file), `${name}.ts`)))
    : require(name)
  new Function("exports", "require", "module", code)(result.exports, localRequire, result)
  return result.exports
}

const { readEventStream, connectOcrStream } = load("api/ocrStream.ts")
const { initialOcrProgress, updateOcrProgress, completeMathPrefix } = load("utils/ocrProgress.ts")
const { recognizeSubmission } = load("api/problemOcr.ts")
const encoder = new TextEncoder()
const region = { coordinate_space: "exif_transposed", image_size: [100, 100], bbox: [0, 0, 100, 100] }

function frame(id, event, data) {
  return `id: ${id}\nevent: ${event}\ndata: ${JSON.stringify(data)}\n\n`
}
function stream(chunks) {
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(typeof chunk === "string" ? encoder.encode(chunk) : chunk)
      controller.close()
    },
  })
}
function sse(body) {
  return new Response(body, { headers: { "Content-Type": "text/event-stream" } })
}
function job(status = "running") {
  return { id: "job-1", status, selected_region: region, error_message: null }
}
function json(value) {
  return Response.json(value)
}
function mockFetch(t, implementation) {
  const original = global.fetch
  const window = global.window
  global.fetch = implementation
  global.window = globalThis
  t.after(() => { global.fetch = original; global.window = window })
}

test("SSE handles split UTF-8, CRLF, comments and multi-line data", async () => {
  const raw = ': heartbeat\r\nretry: 1000\r\n\r\nid: 12\r\nevent: delta\r\ndata: {"text":\r\ndata: "한글"}\r\n\r\n'
  const bytes = encoder.encode(raw)
  const events = []
  await readEventStream(stream([...bytes].map((byte) => Uint8Array.of(byte))), (event) => { events.push(event) })
  assert.equal(events.length, 1)
  assert.equal(events[0].id, "12")
  assert.deepEqual(JSON.parse(events[0].data), { text: "한글" })
})

test("SSE discards an unterminated frame and can stop at a terminal event", async () => {
  const events = []
  await readEventStream(stream([frame(1, "completed", {}), frame(2, "delta", { text: "ignored" })]), (event) => {
    events.push(event)
    return false
  })
  assert.equal(events.length, 1)
  await readEventStream(stream(['id: 3\nevent: delta\ndata: {"text":"unfinished"}']), (event) => { events.push(event) })
  assert.equal(events.length, 1)
})

test("retry text replaces the first attempt and auxiliary deltas stay out of the preview", () => {
  let progress = updateOcrProgress(initialOcrProgress(), "delta", { region_id: "problem", text: "첫 결과", attempt: 1 })
  progress = updateOcrProgress(progress, "delta", { region_id: "auto-formula", text: "보조 수식", attempt: 1 })
  assert.equal(progress.text, "첫 결과")
  progress = updateOcrProgress(progress, "stage", { stage: "retry", attempt: 2 })
  assert.equal(progress.text, "")
  progress = updateOcrProgress(progress, "delta", { region_id: "problem", text: "두 번째 결과", attempt: 2 })
  assert.equal(progress.text, "두 번째 결과")
})

test("incomplete formulas are buffered without exposing partial LaTeX", () => {
  const slash = String.fromCharCode(92)
  for (const value of ["앞 " + slash, "앞 " + slash + "(" + slash + "frac{1", "앞 $$x+", "앞 $x+", "앞 " + slash + "frac{1}{", "앞 x^{"]) {
    assert.equal(completeMathPrefix(value), "앞 ")
  }
  for (const value of ["앞 " + slash + "(" + slash + "frac{1}{2}" + slash + ") 뒤", "$$x^2$$", slash + "frac{1+" + slash + "sqrt{x}}{2}", "가격 100" + slash + "$"]) {
    assert.equal(completeMathPrefix(value), value)
  }
})

test("partial text arrives before completion, but the saved server result wins", async (t) => {
  let controller
  let received
  let finished = false
  const partial = new Promise((resolve) => { received = resolve })
  mockFetch(t, async (url) => {
    if (url.endsWith("/ocr")) return json(job())
    if (url.endsWith("/events")) return sse(new ReadableStream({ start(value) { controller = value } }))
    assert.equal(url, "/api/submissions/1")
    return json({ id: 1, latest_input: { content_text: "저장된 최종 본문" } })
  })
  const result = recognizeSubmission({ id: 1 }, new AbortController().signal, () => {}, region, (progress) => {
    if (progress.text === "읽는 중인 본문") received()
  }).then((value) => { finished = true; return value })
  while (!controller) await new Promise((resolve) => setImmediate(resolve))
  controller.enqueue(encoder.encode(frame(1, "delta", { region_id: "problem", text: "읽는 중인 본문" })))
  await partial
  assert.equal(finished, false)
  controller.enqueue(encoder.encode(frame(2, "completed", {})))
  controller.close()
  assert.equal((await result).latest_input.content_text, "저장된 최종 본문")
})

test("reconnection preserves 64-bit IDs and does not append replayed deltas twice", async (t) => {
  let connections = 0
  const snapshots = []
  mockFetch(t, async (url, options) => {
    if (url.endsWith("/ocr")) return json(job())
    if (url.endsWith("/events")) {
      connections++
      if (connections === 1) return sse(stream([frame("9007199254740993", "delta", { text: "첫" })]))
      assert.equal(options.headers["Last-Event-ID"], "9007199254740993")
      assert.equal(options.credentials, "same-origin")
      return sse(stream([
        frame("9007199254740993", "delta", { text: "첫" }),
        frame("9007199254740994", "delta", { text: "번째" }),
        frame("9007199254740995", "completed", {}),
      ]))
    }
    if (url.includes("/ocr-jobs/")) return json(job())
    return json({ id: 1, latest_input: { content_text: "최종" } })
  })
  await recognizeSubmission({ id: 1 }, new AbortController().signal, () => {}, region, (value) => { snapshots.push(value) })
  assert.equal(connections, 2)
  assert.ok(snapshots.some((value) => value.text === "첫번째"))
  assert.ok(snapshots.every((value) => !value.text.includes("첫첫")))
})

test("repeated streaming failures fall back to status polling", async (t) => {
  let connections = 0
  mockFetch(t, async (url) => {
    if (url.endsWith("/ocr")) return json(job())
    if (url.endsWith("/events")) { connections++; throw new TypeError("connection interrupted") }
    if (url.includes("/ocr-jobs/")) return json(job(connections >= 3 ? "completed" : "running"))
    return json({ id: 1, latest_input: { content_text: "저장 결과" } })
  })
  const snapshots = []
  const result = await recognizeSubmission({ id: 1 }, new AbortController().signal, () => {}, region, (value) => { snapshots.push(value) })
  assert.equal(connections, 3)
  assert.equal(result.latest_input.content_text, "저장 결과")
  assert.ok(snapshots.some((value) => value.connection === "polling"))
})

test("authorization failures stop without polling or reconnecting", async (t) => {
  let requests = 0
  mockFetch(t, async (url) => {
    requests++
    if (url.endsWith("/ocr")) return json(job())
    return new Response("", { status: 401 })
  })
  await assert.rejects(recognizeSubmission({ id: 1 }, new AbortController().signal, () => {}, region), { status: 401 })
  assert.equal(requests, 2)
})

test("a failed OCR never turns partial text into a saved submission", async (t) => {
  mockFetch(t, async (url) => {
    if (url.endsWith("/ocr")) return json(job())
    assert.ok(url.endsWith("/events"))
    return sse(stream([frame(1, "delta", { text: "일부 내용" }), frame(2, "failed", { error_message: "OCR failed" })]))
  })
  await assert.rejects(recognizeSubmission({ id: 1 }, new AbortController().signal, () => {}, region), /OCR failed/)
})

test("abort and idle timeout close the stream connection", async (t) => {
  mockFetch(t, async (_url, options) => sse(new ReadableStream({
    start(controller) {
      options.signal.addEventListener("abort", () => controller.error(new DOMException("Aborted", "AbortError")), { once: true })
    },
  })))
  const parent = new AbortController()
  const running = connectOcrStream("job-1", "0", parent.signal, () => {}, () => {})
  parent.abort()
  await assert.rejects(running, { name: "AbortError" })
  await assert.rejects(connectOcrStream("job-1", "0", new AbortController().signal, () => {}, () => {}, 5), { name: "AbortError" })
})
