import { useEffect, useRef, useState } from "react"
import { MathfieldElement } from "mathlive"
import katex from "katex"
import "mathlive/fonts.css"

// 폰트는 위에서 가져온 CSS를 통해 로딩합니다.
MathfieldElement.fontsDirectory = null
MathfieldElement.soundsDirectory = null

type MathPadProps = {
  initialLatex: string
  onApply: (latex: string) => void
  onCancel: () => void
}

const shortcuts = [
  { label: "제곱", template: "#@^{2}" },
  { label: "거듭제곱", template: "#@^{#?}" },
  { label: "루트", template: "\\sqrt{#0}" },
  { label: "분수", template: "\\frac{#0}{#?}" },
]

export function MathPad({
  initialLatex,
  onApply,
  onCancel,
}: MathPadProps) {
  const hostRef = useRef<HTMLDivElement>(null)
  const keyboardRef = useRef<HTMLDivElement>(null)
  const fieldRef = useRef<MathfieldElement | null>(null)
  const [error, setError] = useState("")

  useEffect(() => {
    const host = hostRef.current
    const keyboardHost = keyboardRef.current
    if (!host || !keyboardHost) return

    const field = new MathfieldElement()
    field.value = initialLatex
    field.mathVirtualKeyboardPolicy = "manual"
    field.setAttribute("aria-label", "수식 입력")

    const keyboard = window.mathVirtualKeyboard
    const previousLayouts = keyboard.layouts

    // 키보드를 편집창 안에 표시합니다.
    keyboard.container = keyboardHost
    keyboard.layouts = ["numeric", "symbols", "alphabetic"]

    const showKeyboard = () => keyboard.show()
    const clearError = () => setError("")

    const resizeKeyboard = () => {
      keyboardHost.style.height =
        `${Math.ceil(keyboard.boundingRect.height)}px`
    }

    field.addEventListener("focusin", showKeyboard)
    field.addEventListener("input", clearError)
    keyboard.addEventListener("geometrychange", resizeKeyboard)

    host.appendChild(field)
    fieldRef.current = field

    field.focus()
    keyboard.show()
    resizeKeyboard()

    return () => {
      field.removeEventListener("focusin", showKeyboard)
      field.removeEventListener("input", clearError)
      keyboard.removeEventListener("geometrychange", resizeKeyboard)

      keyboard.hide()
      keyboard.container = null
      keyboard.layouts = previousLayouts

      field.remove()
      fieldRef.current = null
    }
  }, [initialLatex])

  function insertTemplate(template: string) {
    const field = fieldRef.current
    if (!field) return

    field.focus()
    field.insert(template, { selectionMode: "placeholder" })
  }

  function apply() {
    const field = fieldRef.current
    if (!field) return

    const latex = field.value

    if (
      !latex.trim() ||
      field.errors.length > 0 ||
      latex.includes("\\placeholder")
    ) {
      setError("수식의 빈칸과 입력 내용을 확인해 주세요.")
      return
    }

    // 문제 화면에서도 정상적으로 표시할 수 있는지 검사합니다.
    try {
      katex.renderToString(latex, {
        throwOnError: true,
        trust: false,
      })
    } catch {
      setError("표시할 수 없는 수식이 포함되어 있습니다.")
      return
    }

    onApply(latex)
  }

  return (
    <div className="review-math-pad">
      <div className="review-math-body">
        <h2 id="review-math-title">수식 수정</h2>

        <p className="review-help">
          수정할 위치나 빈칸을 누른 뒤 입력해 주세요.
        </p>

        <div ref={hostRef} className="review-math-field" />

        <div className="review-math-shortcuts">
          {shortcuts.map(({ label, template }) => (
            <button
              key={label}
              type="button"
              onPointerDown={(event) => event.preventDefault()}
              onClick={() => insertTemplate(template)}
            >
              {label}
            </button>
          ))}
        </div>

        {error && (
          <p className="review-error" role="alert">
            {error}
          </p>
        )}

        <div className="review-editor-actions">
          <button type="button" onClick={onCancel}>
            취소
          </button>
          <button
            type="button"
            className="review-apply"
            onClick={apply}
          >
            수정 적용
          </button>
        </div>
      </div>

      <div ref={keyboardRef} className="review-math-keyboard" />
    </div>
  )
}