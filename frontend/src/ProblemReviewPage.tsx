import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { MathFormula } from "./components/MathFormula";
import type { ContentPart, ProblemReview } from "./types/problemReview";
import {
  recognizePhoto,
  createReviewProblem,
  loadReview,
  readReviewPhoto,
  saveReview,
  serializeReviewContent,
  ReviewApiError,
} from "./api/problemOcr";
import type { OcrPhase, Submission } from "./api/problemOcr";
import { MathPad } from "./components/MathPad";
import { PhotoPicker } from "./components/PhotoPicker";
import "./problem-review.css";

function PartContent({ part }: { part: ContentPart }) {
  if (part.kind === "math") {
    return <MathFormula latex={part.latex} />;
  }

  return <>{part.text}</>;
}

function createEmptyProblem(imageUrl: string | null = null): ProblemReview {
  return { id: "", imageUrl, original: [], draft: [] };
}

function rememberSubmission(id: number | null) {
  const url = new URL(window.location.href);
  if (id === null) url.searchParams.delete("submission");
  else url.searchParams.set("submission", String(id));
  window.history.replaceState(null, "", url);
}

export function ProblemReviewPage() {
const [problem, setProblem] = useState(() => createEmptyProblem());
const [selectedId, setSelectedId] = useState<string | null>(null);

const textDialogRef = useRef<HTMLDialogElement>(null);
const mathDialogRef = useRef<HTMLDialogElement>(null);

const [mathEdit, setMathEdit] = useState<{
  partId: string;
  latex: string;
} | null>(null);

const [textEdit, setTextEdit] = useState<{
  partId: string;
  value: string;
} | null>(null);

const [phase, setPhase] = useState<OcrPhase>("idle");
const [ocrError, setOcrError] = useState("");
const ocrControllerRef = useRef<AbortController | null>(null);
const [submission, setSubmission] = useState<Submission | null>(null);
const [saving, setSaving] = useState<"save" | "confirm" | null>(null);
const [saveNotice, setSaveNotice] = useState("");
const [saveError, setSaveError] = useState("");
const [needsReload, setNeedsReload] = useState(false);
const saveControllerRef = useRef<AbortController | null>(null);
const originalTextRef = useRef("");

const busy =
  phase === "restoring" || phase === "uploading" ||
  phase === "queued" || phase === "running";
const locked = busy || saving !== null;

let draftText = "";
let draftError = "";
try {
  draftText = serializeReviewContent(problem.draft);
  if (submission && !draftText.trim()) draftError = "문제 내용을 입력해 주세요.";
  else if (draftText.length > 24000) draftError = "문제 내용은 24,000자 이하로 입력해 주세요.";
} catch (error) {
  draftError = error instanceof Error ? error.message : "문제 내용을 확인해 주세요.";
}

const hasEdits = !!submission &&
  (!!draftError || draftText !== submission.latest_input?.content_text);
const confirmed = submission?.review_status === "confirmed" && !hasEdits;

useEffect(() => {
  const imageUrl = problem.imageUrl;
  return () => { if (imageUrl) URL.revokeObjectURL(imageUrl); };
}, [problem.imageUrl]);

useEffect(() => {
  const savedId = new URL(window.location.href).searchParams.get("submission");
  if (savedId !== null) {
    const id = Number(savedId);
    if (/^[1-9]\d*$/.test(savedId) && Number.isSafeInteger(id)) {
      void restoreSubmission(id);
    } else {
      setPhase("failed");
      setOcrError("문제 주소를 확인해 주세요.");
    }
  }

  return () => {
    const controller = ocrControllerRef.current;
    ocrControllerRef.current = null;
    controller?.abort();
    const saveController = saveControllerRef.current;
    saveControllerRef.current = null;
    saveController?.abort();
  };
}, []);

async function restoreSubmission(id: number) {
  ocrControllerRef.current?.abort();
  const controller = new AbortController();
  ocrControllerRef.current = controller;
  const timeout = window.setTimeout(() => controller.abort(), 30_000);
  setPhase("restoring");
  setOcrError("");

  try {
    const result = await loadReview(id, controller.signal);
    if (ocrControllerRef.current !== controller) return;
    const imageUrl = URL.createObjectURL(result.photo);
    originalTextRef.current = result.originalText;
    setProblem(createReviewProblem(result.submission, result.originalText, imageUrl));
    setSubmission(result.submission);
    setSelectedId(null);
    setNeedsReload(false);
    setSaveError("");
    setSaveNotice("");
    setPhase("completed");
    rememberSubmission(result.submission.id);
  } catch (error) {
    if (ocrControllerRef.current !== controller) return;
    setPhase("failed");
    setOcrError(controller.signal.aborted
      ? "불러오기 시간이 초과됐습니다. 다시 시도해 주세요."
      : error instanceof Error ? error.message : "저장된 문제를 불러오지 못했습니다.");
  } finally {
    window.clearTimeout(timeout);
    controller.abort();
    if (ocrControllerRef.current === controller) ocrControllerRef.current = null;
  }
}

async function startPhotoRecognition(file: File, imageUrl: string) {
  ocrControllerRef.current?.abort();
  const controller = new AbortController();
  ocrControllerRef.current = controller;

  let timedOut = false;
  const timeout = window.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, 360_000);

  setPhase("uploading");
  setOcrError("");
  setSelectedId(null);
  setProblem(createEmptyProblem(imageUrl));
  setSubmission(null);
  setSaveNotice("");
  setSaveError("");
  setNeedsReload(false);
  originalTextRef.current = "";
  rememberSubmission(null);
  textDialogRef.current?.close();
  mathDialogRef.current?.close();

  try {
    const result = await recognizePhoto(file, controller.signal, (nextPhase) => {
      if (ocrControllerRef.current === controller) setPhase(nextPhase);
    });

    let displayedUrl = imageUrl;
    if (result.image_rotation_ccw) {
      const photo = await readReviewPhoto(result.id, controller.signal);
      if (ocrControllerRef.current !== controller) return;
      displayedUrl = URL.createObjectURL(photo);
    }
    if (ocrControllerRef.current !== controller) return;
    originalTextRef.current = result.latest_input!.content_text;
    setProblem(createReviewProblem(result, originalTextRef.current, displayedUrl));
    setSubmission(result);
    rememberSubmission(result.id);
    setPhase("completed");
  } catch (error) {
    if (ocrControllerRef.current !== controller) return;
    setPhase("failed");
    setOcrError(
      timedOut
        ? "인식 대기 시간을 초과했습니다. 서버 작업은 계속될 수 있습니다."
        : error instanceof Error
          ? error.message
          : "문제 인식에 실패했습니다.",
    );
  } finally {
    window.clearTimeout(timeout);
    if (ocrControllerRef.current === controller) {
      ocrControllerRef.current = null;
    }
  }
}

function choosePhoto(file: File): boolean {
  if (locked || saveControllerRef.current) return false;

  if (
    hasEdits &&
    !window.confirm("사진을 바꾸면 저장하지 않은 수정 내용이 사라집니다. 계속할까요?")
  ) {
    return false;
  }

  void startPhotoRecognition(file, URL.createObjectURL(file));
  return true;
}

async function persistReview(confirm: boolean) {
  const latest = submission?.latest_input;
  if (!submission || !latest || locked || saveControllerRef.current || needsReload) return;
  if ((!confirm && !hasEdits) || (confirm && confirmed)) return;
  if (draftError) { setSaveError(draftError); return; }

  const controller = new AbortController();
  saveControllerRef.current = controller;
  const timeout = window.setTimeout(() => controller.abort(), 30_000);
  setSaving(confirm ? "confirm" : "save");
  setSaveNotice("");
  setSaveError("");

  try {
    const updated = await saveReview(
      submission.id, draftText, latest.version_number, confirm, controller.signal,
    );
    if (saveControllerRef.current !== controller) return;
    if (!updated.latest_input || (confirm && updated.review_status !== "confirmed")) {
      throw new Error("저장 상태를 확인하지 못했습니다. 최신 내용을 다시 불러와 주세요.");
    }

    setSubmission(updated);
    setProblem((current) => createReviewProblem(
      updated, originalTextRef.current, current.imageUrl,
    ));
    setSelectedId(null);
    setSaveNotice(confirm ? "문제 내용 확인을 완료하고 저장했습니다." : "수정본을 저장했습니다.");
  } catch (error) {
    if (saveControllerRef.current !== controller) return;
    if (error instanceof ReviewApiError && error.status === 409) {
      setNeedsReload(true);
      setSaveError("다른 곳에서 내용이 변경되었습니다. 작성 중인 내용은 유지됩니다. 최신 내용을 불러온 뒤 다시 확인해 주세요.");
    } else {
      setSaveError(controller.signal.aborted
        ? "저장 응답을 확인하지 못했습니다. 작성 중인 내용은 유지됩니다. 최신 내용을 불러와 저장 상태를 확인해 주세요."
        : error instanceof Error ? error.message : "저장하지 못했습니다. 다시 시도해 주세요.");
    }
  } finally {
    window.clearTimeout(timeout);
    if (saveControllerRef.current === controller) {
      saveControllerRef.current = null;
      setSaving(null);
    }
  }
}

function reloadLatest() {
  if (!submission || locked) return;
  if (hasEdits && !window.confirm("저장하지 않은 수정 내용을 버리고 최신 내용을 불러올까요?")) return;
  void restoreSubmission(submission.id);
}

const phaseMessage: Record<OcrPhase, string> = {
  idle: "사진을 선택하면 인식한 내용이 여기에 표시됩니다.",
  restoring: "저장된 문제와 원본 사진을 불러오고 있어요.",
  uploading: "사진을 업로드하고 있어요.",
  queued: "문제 인식 순서를 기다리고 있어요.",
  running: "문제 사진을 읽고 있어요. 잠시 기다려 주세요.",
  completed: "인식이 완료됐어요. 원본과 비교해 주세요.",
  failed: "문제 인식을 완료하지 못했어요.",
};

function selectPart(part: ContentPart) {
  if (locked) return;
  setSelectedId(part.id);

  if (part.kind === "text") {
    setTextEdit({
      partId: part.id,
      value: part.text,
    });

    textDialogRef.current?.showModal();
  } else {
    setMathEdit({
      partId: part.id,
      latex: part.latex,
    });

    mathDialogRef.current?.showModal();
  }
}

function applyTextEdit(event: FormEvent<HTMLFormElement>) {
  event.preventDefault();
  if (!textEdit) return;

  const { partId, value } = textEdit;

  setProblem((current) => ({
    ...current,
    draft: current.draft.map((block) => {
      if (block.kind !== "paragraph") return block;

      return {
        ...block,
        parts: block.parts.map((part) => {
          if (part.id === partId && part.kind === "text") {
            return { ...part, text: value };
          }

          return part;
        }),
      };
    }),
  }));

  textDialogRef.current?.close();
  setSaveNotice("");
}

function applyMathEdit(latex: string) {
  if (!mathEdit) return;

  const partId = mathEdit.partId;

  setProblem((current) => ({
    ...current,
    draft: current.draft.map((block) => {
      if (block.kind !== "paragraph") return block;

      return {
        ...block,
        parts: block.parts.map((part) => {
          if (part.id === partId && part.kind === "math") {
            return { ...part, latex };
          }

          return part;
        }),
      };
    }),
  }));

  mathDialogRef.current?.close();
  setSaveNotice("");
}


  const selectedPart = problem.draft
    .flatMap((block) => (
      block.kind === "paragraph" ? block.parts : []
    ))
    .find((part) => part.id === selectedId);

  return (
    <div className="review-page">
      <header className="review-header">
        <span className="review-brand">딛고</span>
        <span className="review-badge">
          {phase === "restoring" ? "불러오는 중" : busy ? "인식 중"
            : confirmed ? "확인 완료" : hasEdits ? "수정 중"
              : phase === "completed" ? "확인 대기" : "문제 등록"}
        </span>
      </header>

      <main className="review-main">
        <div className="review-heading">
          <h1>문제 내용 확인</h1>
          <p>원본과 비교하고, 다른 부분을 눌러 수정해 주세요.</p>
        </div>

        <section
          className="review-section"
          aria-labelledby="review-photo-title"
        >
          <h2 id="review-photo-title">원본 사진</h2>

          <PhotoPicker onSelect={choosePhoto} imageUrl={problem.imageUrl} disabled={locked} />
          {submission?.image_orientation?.status === "corrected" && (
            <p className="review-help">사진 방향을 자동으로 보정했어요.</p>
          )}
          {(submission?.image_orientation?.status === "uncertain" ||
            submission?.image_orientation?.status === "unavailable") && (
            <p className="review-error" role="status">
              사진 방향을 확실하게 판단하지 못했어요. 방향과 인식 내용을 확인해 주세요.
            </p>
          )}
        </section>

        <section
          className="review-section"
          aria-labelledby="review-content-title"
        >
          <div className="review-section-heading">
            <h2 id="review-content-title">인식한 내용</h2>
            <p>
              {phase === "completed"
                ? "문장이나 수식을 눌러 수정해 주세요."
                : "사진을 선택하면 문제 내용을 인식합니다."}
            </p>
          </div>

          <p className="review-help" role="status">
            {confirmed && phase === "completed" ? "문제 내용 확인이 완료됐어요."
              : phase === "completed" && hasEdits ? "수정한 내용을 저장하거나 확인을 완료해 주세요."
                : phaseMessage[phase]}
          </p>
          {ocrError && <p className="review-error" role="alert">{ocrError}</p>}

          <div className="review-content" aria-busy={busy}>
            {problem.draft.length === 0 && (
              <p className="review-help">
                {busy ? "인식 결과를 기다리고 있어요." : "인식한 내용이 아직 없습니다."}
              </p>
            )}
            {problem.draft.map((block) => {
              if (block.kind !== "paragraph") return null;

              return (
                <p key={block.id} className="review-paragraph">
                  {block.parts.map((part) => part.kind === "text" ? (
                    <span
                      key={part.id}
                      role="button"
                      tabIndex={locked ? -1 : 0}
                      aria-disabled={locked}
                      aria-pressed={selectedId === part.id}
                      className="review-part review-text-part"
                      onClick={() => selectPart(part)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter" || event.key === " ") {
                          event.preventDefault();
                          selectPart(part);
                        }
                      }}
                    >
                      {part.text}
                    </span>
                  ) : (
                    <button
                      key={part.id}
                      type="button"
                      disabled={locked}
                      className="review-part"
                      aria-pressed={selectedId === part.id}
                      onClick={() => selectPart(part)}
                    >
                      <PartContent part={part} />
                    </button>
                  ))}
                </p>
              );
            })}
          </div>
        </section>

        <section
          className="review-section review-selection"
          aria-labelledby="review-selection-title"
        >
          <h2 id="review-selection-title">선택한 부분</h2>

          <div aria-live="polite" aria-atomic="true">
            {selectedPart ? (
              <>
                <p className="review-selection-kind">
                  {selectedPart.kind === "math" ? "수식" : "본문"}
                </p>
                <div className="review-selected-content">
                  <PartContent part={selectedPart} />
                </div>
              </>
            ) : (
              <p className="review-selection-empty">
                위에서 확인할 부분을 선택해 주세요.
              </p>
            )}
          </div>
        </section>

        {submission?.latest_input && (
          <section className="review-completion" aria-label="수정본 저장과 확인 완료" aria-busy={saving !== null}>
            <p className="review-help">
              {hasEdits ? "아직 저장하지 않은 수정 내용이 있어요."
                : confirmed ? "확인 완료한 내용이 저장되어 있어요."
                  : "원본과 비교한 뒤 문제 내용 확인을 완료해 주세요."}
            </p>
            {draftError && <p className="review-error" role="alert">{draftError}</p>}
            {saveError && <p className="review-error" role="alert">{saveError}</p>}
            {saveNotice && <p className="review-help" role="status">{saveNotice}</p>}
            {needsReload && (
              <button type="button" className="review-reload" disabled={locked} onClick={reloadLatest}>
                최신 내용 불러오기
              </button>
            )}
            <button type="button" className="review-save"
              disabled={locked || !hasEdits || !!draftError || needsReload}
              onClick={() => void persistReview(false)}>
              {saving === "save" ? "저장 중…" : "수정본 저장"}
            </button>
            <button type="button" className="review-confirm"
              disabled={locked || confirmed || !!draftError || needsReload}
              onClick={() => void persistReview(true)}>
              {saving === "confirm" ? "확인 완료 처리 중…" : confirmed ? "확인 완료됨" : "문제 내용 확인 완료"}
            </button>
          </section>
        )}
      </main>
      <dialog
        ref={textDialogRef}
        className="review-editor"
        aria-labelledby="review-editor-title"
        onClose={() => setTextEdit(null)}
      >
        <form onSubmit={applyTextEdit}>
          <h2 id="review-editor-title">본문 수정</h2>

          <label htmlFor="review-text-input">
            수정할 내용
          </label>

          <textarea
            id="review-text-input"
            value={textEdit?.value ?? ""}
            onChange={(event) => {
              const value = event.currentTarget.value;

              setTextEdit((current) => (
                current ? { ...current, value } : null
              ));
            }}
            rows={4}
            maxLength={24000}
            required
            autoFocus
          />

          <div className="review-editor-actions">
            <button
              type="button"
              onClick={() => textDialogRef.current?.close()}
            >
              취소
            </button>

            <button type="submit" className="review-apply">
              수정 적용
            </button>
          </div>
        </form>
      </dialog>
      <dialog
        ref={mathDialogRef}
        className="review-editor review-math-editor"
        aria-labelledby="review-math-title"
        onClose={() => setMathEdit(null)}
      >
        {mathEdit && (
          <MathPad
            key={mathEdit.partId}
            initialLatex={mathEdit.latex}
            onApply={applyMathEdit}
            onCancel={() => mathDialogRef.current?.close()}
          />
        )}
      </dialog>
    </div>
  );
}
