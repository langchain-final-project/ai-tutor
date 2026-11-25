// 인식된 내용에 LaTeX 명령어 사용자에게 노출되지 않게끔

type LatexPiece =
  | { kind: "text"; text: string }
  | { kind: "math"; latex: string; raw: string }

const spacingCommand =
  /^\\(?:quad|qquad|enspace|thinspace|medspace|thickspace)\b/

export function displayLatexSpacing(text: string): string {
  return text
    .replace(/\\qquad\b/g, "\u2003\u2003")
    .replace(/\\quad\b/g, "\u2003")
    .replace(/\\(?:enspace|thinspace|medspace|thickspace)\b|\\[,;:]/g, "\u2009")
    .replace(/\\!/g, "")
    .replace(/\\ /g, " ")
}

// 중첩된 분수·루트의 중괄호까지 함께 읽습니다.
function groupEnd(text: string, start: number): number {
  let depth = 0

  for (let i = start; i < text.length; i++) {
    if (text[i] === "\\") {
      i++
      continue
    }

    if (text[i] === "{") depth++
    if (text[i] === "}" && --depth === 0) return i + 1
  }

  return -1
}

function bareMathEnd(text: string, start: number): number {
  let end = start

  while (end < text.length) {
    const char = text[end]

    if (char === "\n" || char === "\r") break

    if (char === "{") {
      const next = groupEnd(text, end)
      if (next < 0) break

      end = next
      continue
    }

    if (char === "\\") {
      const rest = text.slice(end)
      if (spacingCommand.test(rest)) break

      const command = /^\\[A-Za-z]+/.exec(rest)
      end += command ? command[0].length : Math.min(2, rest.length)
      continue
    }

    if (/[A-Za-z0-9\s+\-*/=<>()[\]_^|&:.,πθ≤≥≠×÷°]/.test(char)) {
      end++
      continue
    }

    break
  }

  while (end > start && /\s/.test(text[end - 1])) end--

  return end
}

export function splitLooseLatex(text: string): LatexPiece[] {
  const pieces: LatexPiece[] = []
  const marker = /\\[A-Za-z]+|[A-Za-z0-9]+\s*[_^]/g
  let cursor = 0

  for (const match of text.matchAll(marker)) {
    const start = match.index

    if (start < cursor || spacingCommand.test(text.slice(start))) {
      continue
    }

    const end = bareMathEnd(text, start)
    if (end <= start) continue

    if (start > cursor) {
      pieces.push({
        kind: "text",
        text: text.slice(cursor, start),
      })
    }

    const raw = text.slice(start, end)
    pieces.push({ kind: "math", latex: raw, raw })
    cursor = end
  }

  if (cursor < text.length) {
    pieces.push({
      kind: "text",
      text: text.slice(cursor),
    })
  }

  return pieces
}