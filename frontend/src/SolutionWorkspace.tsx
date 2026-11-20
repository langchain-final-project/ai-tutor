import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { ApiError } from "./auth";
import { solutionApi, solutionMessage } from "./api/solutionFlow";
import type { FeedbackView, ProblemInput, Step, Submission, Verification, Work } from "./api/solutionFlow";

type Props = {
  view: "home" | "questions" | "solutions";
  submissionId: string | null;
  loginUrl: string;
  sourcePhotoId?: string | null;
};
type Snapshot = {
  submission: Submission;
  work: Work | null;
  result: Verification | null;
  history: Verification[];
  views: FeedbackView[];
  inputs: ProblemInput[];
};
type Draft = { equation: string; steps: Step[] };

const newStep = (): Step => ({ id: crypto.randomUUID(), text: "", state: "active" });
const writtenSteps = (steps: Step[]) => steps.filter((step) => step.text.trim() || step.state === "uncertain");
const sameSteps = (a: Step[], b: Step[]) => JSON.stringify(a) === JSON.stringify(b);

function dateLabel(value: string) {
  const date = new Date(value.includes("T") ? value : `${value.replace(" ", "T")}Z`);
  return Number.isNaN(date.getTime()) ? "저장된 기록" : new Intl.DateTimeFormat("ko-KR", {
    month: "long", day: "numeric", hour: "2-digit", minute: "2-digit",
  }).format(date);
}

function resultSummary(result: Verification) {
  const value = result.evaluation.verification;
  if (value.action === "unsupported") return { title: "지원하는 식인지 확인해 주세요", tone: "is-warning" };
  if (value.completion === "no_work") return { title: "첫 풀이를 시작해 보세요", tone: "" };
  if (value.earliest_unresolved_step_id || value.action === "ask_confirmation") {
    return { title: "먼저 입력을 확인해 주세요", tone: "is-warning" };
  }
  if (value.judgement === "incorrect") return { title: "다시 확인할 풀이가 있어요", tone: "is-error" };
  if (value.judgement === "correct" && value.completion === "complete" && !value.first_error_step_id) {
    return { title: "정답과 검산을 확인했어요", tone: "is-success" };
  }
  if (value.judgement === "correct") return { title: "여기까지 유효한 풀이예요", tone: "" };
  return { title: "풀이를 확인해 주세요", tone: "is-warning" };
}

async function readSnapshot(id: number, signal?: AbortSignal): Promise<Snapshot> {
  const before = await solutionApi.get(id, signal);
  if (before.input_kind !== "text") {
    return { submission: before, work: null, result: null, history: [], views: [], inputs: [] };
  }
  const [work, result, history, views, inputs] = await Promise.all([
    solutionApi.getWork(id, signal), solutionApi.currentVerification(id, signal),
    solutionApi.verificationHistory(id, signal), solutionApi.feedbackViews(id, signal),
    solutionApi.inputHistory(id, signal),
  ]);
  const after = await solutionApi.get(id, signal);
  if (before.latest_input?.id !== after.latest_input?.id ||
      before.latest_input?.confirmed_at !== after.latest_input?.confirmed_at ||
      (result && (result.problem_version_number !== after.latest_input?.version_number ||
                  result.work_version_number !== (work?.version_number ?? 0) || !result.is_current))) {
    throw new ApiError(409, "저장 내용이 변경되었습니다.");
  }
  return { submission: after, work, result, history, views, inputs };
}

export function SolutionWorkspace({ view, submissionId, loginUrl, sourcePhotoId = null }: Props) {
  const [questions, setQuestions] = useState<Submission[]>([]);
  const [sourcePhoto, setSourcePhoto] = useState<Submission | null>(null);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [newEquation, setNewEquation] = useState("");
  const [equation, setEquation] = useState("");
  const [steps, setSteps] = useState<Step[]>([newStep()]);
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [conflict, setConflict] = useState(false);
  const [backup, setBackup] = useState<Draft | null>(null);
  const [viewError, setViewError] = useState(false);
  const alive = useRef(false);
  const controller = useRef<AbortController | null>(null);
  const busy = useRef(false);
  const readNumber = useRef(0);
  const shownResults = useRef(new Set<number>());
  const currentResultId = useRef<number | undefined>(undefined);
  currentResultId.current = snapshot?.result?.id;

  const id = submissionId && /^[1-9]\d*$/.test(submissionId) && Number.isSafeInteger(Number(submissionId))
    ? Number(submissionId) : null;
  const sourceId = sourcePhotoId && /^[1-9]\d*$/.test(sourcePhotoId) && Number.isSafeInteger(Number(sourcePhotoId))
    ? Number(sourcePhotoId) : null;
  const input = snapshot?.submission.latest_input;
  const problemDirty = snapshot !== null && equation !== (input?.content_text ?? "");
  const workDirty = snapshot !== null && !sameSteps(writtenSteps(steps), snapshot.work?.steps ?? []);
  const dirty = submissionId === null ? newEquation.trim().length > 0 : problemDirty || workDirty;
  const result = !dirty && snapshot?.result?.is_current ? snapshot.result : null;
  const disabled = loading || pending !== "";

  function fail(reason: unknown) {
    if (!alive.current || (reason instanceof Error && reason.name === "AbortError")) return;
    if (reason instanceof ApiError && reason.status === 401) {
      setSnapshot(null); setQuestions([]); setEquation(""); setSteps([]); setBackup(null);
      setSourcePhoto(null);
      location.replace(loginUrl);
      return;
    }
    if (reason instanceof ApiError && reason.status === 404) {
      setSnapshot(null); setEquation(""); setSteps([]); setBackup(null);
      setSourcePhoto(null);
    }
    if (reason instanceof ApiError && reason.status === 409) {
      setConflict(true);
      setSnapshot((current) => current && ({ ...current, result: null }));
      setViewError(false);
    }
    setError(solutionMessage(reason));
  }

  function applySnapshot(next: Snapshot) {
    setSnapshot(next);
    setEquation(next.submission.latest_input?.content_text ?? "");
    setSteps(next.work?.steps.length ? next.work.steps.map((step) => ({ ...step })) : [newStep()]);
    setConflict(false); setViewError(false);
  }

  async function load() {
    const number = ++readNumber.current;
    setLoading(true);
    try {
      if (submissionId === null) {
        if (sourcePhotoId !== null && !sourceId) throw new Error("사진 문제 주소를 확인해 주세요.");
        const [list, source] = await Promise.all([
          solutionApi.list(controller.current?.signal),
          sourceId ? solutionApi.get(sourceId, controller.current?.signal) : Promise.resolve(null),
        ]);
        if (source && (source.input_kind !== "photo" || !source.latest_input?.confirmed_at)) {
          throw new ApiError(409, "사진 문제 내용을 먼저 확인해 주세요.");
        }
        if (alive.current && number === readNumber.current) {
          setQuestions(list); setSourcePhoto(source); setConflict(false);
        }
      } else if (id === null) {
        throw new Error("문제 주소가 올바르지 않습니다. 질문 내역에서 다시 선택해 주세요.");
      } else {
        const next = await readSnapshot(id, controller.current?.signal);
        if (alive.current && number === readNumber.current) applySnapshot(next);
      }
    } catch (reason) {
      if (number === readNumber.current) fail(reason);
    } finally {
      if (alive.current && number === readNumber.current) setLoading(false);
    }
  }

  useEffect(() => {
    alive.current = true;
    controller.current = new AbortController();
    void load();
    return () => { alive.current = false; controller.current?.abort(); };
  }, [submissionId, sourcePhotoId]);

  useEffect(() => {
    if (!dirty) return;
    function warn(event: BeforeUnloadEvent) { event.preventDefault(); event.returnValue = ""; }
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  async function markViewed(value: Verification) {
    if (!id || shownResults.current.has(value.id)) return;
    shownResults.current.add(value.id);
    try {
      const saved = await solutionApi.recordFeedbackView(id, value.id, controller.current?.signal);
      if (alive.current) {
        setSnapshot((current) => current && ({ ...current,
          views: [...current.views.filter((item) => item.id !== saved.id), saved],
        }));
        setViewError(false);
      }
    } catch (reason) {
      shownResults.current.delete(value.id);
      if (reason instanceof ApiError && (reason.status === 401 || reason.status === 404)) fail(reason);
      else if (alive.current && reason instanceof ApiError && reason.status === 409) {
        if (currentResultId.current === value.id) {
          setSnapshot((current) => current?.result?.id === value.id ? { ...current, result: null } : current);
          fail(reason);
        }
      }
      else if (alive.current && !(reason instanceof Error && reason.name === "AbortError")) setViewError(true);
    }
  }

  useEffect(() => {
    if (result && !loading) void markViewed(result);
  }, [result?.id, loading]);

  async function perform(label: string, action: () => Promise<void>) {
    if (busy.current || loading || conflict) return;
    busy.current = true;
    setPending(label); setError(""); setMessage("");
    try { await action(); }
    catch (reason) { fail(reason); }
    finally {
      busy.current = false;
      if (alive.current) setPending("");
    }
  }

  function create(event: FormEvent) {
    event.preventDefault();
    if (!newEquation.trim()) return;
    if (sourcePhotoId !== null && !sourcePhoto?.latest_input?.confirmed_at) return;
    void perform("문제를 저장하고 있어요…", async () => {
      const saved = sourcePhotoId !== null
        ? await solutionApi.createFromPhoto(sourcePhoto!.id, newEquation.trim(), sourcePhoto!.latest_input!.version_number, controller.current?.signal)
        : await solutionApi.create(newEquation.trim(), controller.current?.signal);
      if (alive.current) location.hash = `#history/solutions/${saved.id}`;
    });
  }

  function confirmProblem(event: FormEvent) {
    event.preventDefault();
    if (!id || !input || !equation.trim()) return;
    void perform("문제를 확인하고 있어요…", async () => {
      const saved = await solutionApi.confirmProblem(id, equation.trim(), input.version_number, controller.current?.signal);
      if (alive.current) {
        const changed = saved.latest_input?.id !== input.id;
        setSnapshot((current) => current && ({ ...current, submission: saved,
          work: changed ? null : current.work, result: null,
          inputs: saved.latest_input ? [...current.inputs.filter((item) => item.id !== saved.latest_input?.id), saved.latest_input] : current.inputs,
        }));
        setEquation(saved.latest_input?.content_text ?? "");
        if (changed) setSteps([newStep()]);
      }
      const next = await readSnapshot(id, controller.current?.signal);
      if (alive.current) {
        applySnapshot(next);
        setMessage("문제를 확인했어요. 이제 풀이를 한 줄씩 입력해 보세요.");
      }
    });
  }

  function inspectWork(event: FormEvent) {
    event.preventDefault();
    if (!id || !input?.confirmed_at || problemDirty || !writtenSteps(steps).length) return;
    void perform("풀이를 저장하고 확인하고 있어요…", async () => {
      setSnapshot((current) => current && ({ ...current, result: null }));
      const saved = await solutionApi.saveWork(id, input.version_number, snapshot?.work?.version_number ?? 0,
        writtenSteps(steps), controller.current?.signal);
      if (alive.current) setSnapshot((current) => current && ({ ...current, work: saved, result: null }));
      const confirmed = await solutionApi.confirmWork(id, input.version_number, saved.version_number, controller.current?.signal);
      if (alive.current) setSnapshot((current) => current && ({ ...current, work: confirmed, result: null }));
      const checked = await solutionApi.verify(id, input.version_number, confirmed.version_number, controller.current?.signal);
      if (alive.current) {
        setSteps(saved.steps.map((step) => ({ ...step })));
        setSnapshot((current) => current && ({ ...current, result: checked,
          history: [checked, ...current.history.filter((item) => item.id !== checked.id)],
        }));
      }
    });
  }

  function firstGuidance() {
    if (!id || !input?.confirmed_at || dirty) return;
    void perform("시작 안내를 확인하고 있어요…", async () => {
      const checked = await solutionApi.verify(id, input.version_number, snapshot?.work?.version_number ?? 0,
        controller.current?.signal);
      if (alive.current) setSnapshot((current) => current && ({ ...current, result: checked,
        history: [checked, ...current.history.filter((item) => item.id !== checked.id)],
      }));
    });
  }

  function reread() {
    if (busy.current || loading) return;
    if (dirty && snapshot) setBackup({ equation, steps: steps.map((step) => ({ ...step })) });
    setError(""); setMessage("");
    void load();
  }

  const summary = result ? resultSummary(result) : null;
  const verdict = result?.evaluation.verification;

  return (
    <div className="solution-workspace" aria-busy={disabled}>
      {error && <div className="solution-notice is-error" role="alert">
        <p>{error}</p>
        {conflict && <p>작성 중인 내용은 입력란에 그대로 남아 있어요.</p>}
        <button className="solution-button is-secondary" type="button" disabled={disabled} onClick={reread}>
          최신 저장 내용 확인
        </button>
      </div>}
      {message && <p className="solution-notice" role="status">{message}</p>}
      {pending && <p className="solution-loading" role="status">{pending}</p>}
      {loading && <p className="solution-loading" role="status">저장한 내용을 불러오고 있어요…</p>}

      {!loading && submissionId === null && <>
        <section className="solution-intro">
          <p className="solution-eyebrow">한 줄씩, 내 힘으로</p>
          <h2>{sourcePhotoId !== null ? "사진에서 확인한 문제를 이어 풀어요" : view === "home" ? "막힌 식부터 다시 시작해요" : "내 문제를 이어 풀어요"}</h2>
          <p>문제를 직접 입력하고, 마지막으로 확인한 풀이에서 다음 한 걸음을 찾아보세요.</p>
          <a className="solution-button" href="#tutor/new">질문·단계별 힌트로 AI 튜터 시작</a>
        </section>
        <div className="solution-grid">
          <section className="solution-card" aria-labelledby="new-problem-title">
            <h2 id="new-problem-title">내 문제 풀기</h2>
            {sourcePhotoId === null && <a className="solution-button is-secondary" href="/review">사진으로 문제 가져오기</a>}
            {sourcePhoto && <div className="solution-notice">
              <p>사진에서 확인한 원문은 그대로 보존돼요. 풀이에 사용할 일차등식 하나를 직접 입력해 주세요.</p>
              <a className="solution-inline-link" href={`/review?submission=${sourcePhoto.id}`}>출처 사진과 확인한 내용 보기 →</a>
            </div>}
            <p className="solution-help">미지수 하나인 일차방정식을 입력해 주세요. 괄호·소수·분수도 사용할 수 있어요.</p>
            <form className="solution-form" onSubmit={create}>
              <label className="solution-label" htmlFor="new-equation">문제의 식</label>
              <input id="new-equation" className="solution-input solution-equation" value={newEquation}
                onChange={(event) => setNewEquation(event.target.value)} placeholder="예: 3(x-2)=12"
                maxLength={512} required autoComplete="off" disabled={disabled || conflict || (sourcePhotoId !== null && !sourcePhoto)} />
              <p className="solution-help">분수 계수는 (1/2)*x처럼 괄호와 곱셈 기호를 써 주세요.</p>
              <button className="solution-button" disabled={disabled || conflict || !newEquation.trim() || (sourcePhotoId !== null && !sourcePhoto)}>
                {sourcePhotoId !== null ? "이 식으로 풀이 등록하기" : "문제 등록하기"}
              </button>
            </form>
          </section>
          <section className="solution-card" aria-labelledby="my-questions-title">
            <div className="solution-section-header">
              <h2 id="my-questions-title">{view === "solutions" ? "이어서 풀 문제" : "내 질문 내역"}</h2>
              <button className="solution-button is-quiet" type="button" onClick={reread} disabled={disabled}>새로고침</button>
            </div>
            <p className="solution-help">사진과 식으로 등록한 최근 문제 100개를 보여드려요.</p>
            {!questions.length && !error && <p className="solution-empty">아직 등록한 문제가 없어요. 첫 문제를 등록해 보세요.</p>}
            <ul className="solution-list">
              {questions.map((question) => <li key={question.id}>
                <a className="solution-list-item" href={question.input_kind === "photo" ? `/review?submission=${question.id}` : `#history/solutions/${question.id}`}>
                  <span className="solution-equation">{question.latest_input?.content_text ?? question.original_filename ?? "사진 인식 대기"}</span>
                  <span className="solution-meta">{dateLabel(question.created_at)} · {question.input_kind === "photo" ? "사진" : "직접 입력"} · {question.review_status === "confirmed" ? "문제 확인됨" : "문제 확인 대기"}</span>
                  <span className="solution-inline-link">{question.input_kind === "photo" ? "사진 확인 열기" : "풀이 열기"} →</span>
                </a>
              </li>)}
            </ul>
          </section>
        </div>
      </>}

      {!loading && snapshot && snapshot.submission.input_kind !== "text" && <div className="solution-card">
        <p>사진으로 등록한 문제예요. 원본과 인식한 내용은 문제 확인 화면에서 이어 볼 수 있어요.</p>
        <a className="solution-inline-link" href={`/review?submission=${snapshot.submission.id}`}>사진과 문제 내용 확인하기 →</a>
      </div>}

      {!loading && snapshot?.submission.input_kind === "text" && <>
        {snapshot.submission.source_photo && <p className="solution-notice">
          <a href={`/review?submission=${snapshot.submission.source_photo.submission_id}`}>이 풀이의 출처 사진 확인하기 →</a>
          <span className="solution-help">연결한 사진 입력 버전: {snapshot.submission.source_photo.input_version_number}</span>
        </p>}
        <div className="solution-section-header">
          <a className="solution-inline-link" href="#history/questions">← 내 질문 내역</a>
          <a className="solution-inline-link" href={`#tutor/from-submission/${snapshot.submission.id}`}>이 문제로 AI 튜터 시작 →</a>
          <button className="solution-button is-quiet" type="button" onClick={reread} disabled={disabled}>저장된 내용 불러오기</button>
        </div>
        {backup && <section className="solution-notice is-warning" aria-label="불러오기 전 작성한 내용">
          <h2>불러오기 전 작성한 내용</h2>
          <p>저장된 내용과 비교할 수 있도록 남겨 두었어요. 필요한 부분을 복사해 다시 작성해 주세요.</p>
          <pre className="solution-equation">{backup.equation}{"\n"}{backup.steps.map((step) => step.text).join("\n")}</pre>
          <button className="solution-button is-quiet" type="button" onClick={() => setBackup(null)}>이 참고 내용 닫기</button>
        </section>}
        <div className="solution-grid">
          <div>
            <section className="solution-card" aria-labelledby="problem-title">
              <p className="solution-eyebrow">01 · 문제 확인</p>
              <h2 id="problem-title">이 식을 풀어볼까요?</h2>
              <form className="solution-form" onSubmit={confirmProblem}>
                <label className="solution-label" htmlFor="problem-equation">문제의 식</label>
                <input id="problem-equation" className="solution-input solution-equation" value={equation}
                  onChange={(event) => setEquation(event.target.value)} maxLength={512} autoComplete="off"
                  disabled={disabled || conflict} required />
                <p className="solution-help">문제의 식을 바꾸면 기존 풀이와 판정은 이전 기록으로 남고, 새 풀이를 시작해요.</p>
                <button className="solution-button is-secondary" disabled={disabled || conflict || !equation.trim() || (!!input?.confirmed_at && !problemDirty)}>
                  {input?.confirmed_at && !problemDirty ? "문제 확인 완료" : "이 문제로 확인하기"}
                </button>
              </form>
            </section>

            <section className="solution-card" aria-labelledby="work-title">
              <p className="solution-eyebrow">02 · 한 줄씩 풀기</p>
              <h2 id="work-title">내가 푼 식을 적어 주세요</h2>
              {(!input?.confirmed_at || problemDirty) && <p className="solution-notice is-warning">위의 문제를 먼저 확인한 뒤 풀이를 제출해 주세요.</p>}
              {dirty && snapshot.result && <p className="solution-notice">내용을 수정 중이에요. 현재 작성한 풀이를 다시 제출하면 새 판정을 확인할 수 있어요.</p>}
              <form className="solution-form" onSubmit={inspectWork}>
                <ol className="solution-work-lines">
                  {steps.map((step, index) => {
                    const checked = verdict?.steps.find((item) => item.step_id === step.id);
                    const isError = checked?.judgement === "incorrect" && !checked.skipped;
                    const unresolved = checked?.judgement === "undetermined" && !checked.skipped;
                    return <li key={step.id} className={`solution-line ${isError ? "is-error" : ""} ${unresolved ? "is-unresolved" : ""} ${step.state === "deleted" ? "is-deleted" : ""}`}>
                      <span className="solution-line-number" aria-hidden="true">{index + 1}</span>
                      <label className="solution-line-input">
                        <span className="solution-label">풀이 {index + 1}번째 줄</span>
                        <input className="solution-input solution-equation" value={step.text} maxLength={512}
                          onChange={(event) => setSteps((current) => current.map((item) => item.id === step.id ? { ...item, text: event.target.value } : item))}
                          placeholder={index === 0 ? "예: x-2=4" : "다음 식 또는 최종 답"}
                          autoComplete="off" disabled={disabled || conflict || !input?.confirmed_at || problemDirty} />
                      </label>
                      <label className="solution-line-state">
                        <span className="solution-label">줄 상태</span>
                        <select className="solution-input" value={step.state} disabled={disabled || conflict || !input?.confirmed_at || problemDirty}
                          onChange={(event) => setSteps((current) => current.map((item) => item.id === step.id ? { ...item, state: event.target.value as Step["state"] } : item))}>
                          <option value="active">사용</option><option value="deleted">지운 줄</option><option value="uncertain">확인 필요</option>
                        </select>
                      </label>
                      {checked && <p className="solution-line-result">{checked.skipped ? "지운 줄 · 판정에서 제외" :
                        checked.judgement === "correct" ? "유효한 식" : checked.judgement === "incorrect" ? "앞 식과 비교해 확인해 주세요" : "표기를 확인해 주세요"}</p>}
                    </li>;
                  })}
                </ol>
                <div className="solution-actions">
                  <button className="solution-button is-secondary" type="button" disabled={disabled || conflict || !input?.confirmed_at || problemDirty || steps.length >= 100}
                    onClick={() => setSteps((current) => [...current, newStep()])}>+ 풀이 줄 추가</button>
                  <button className="solution-button" disabled={disabled || conflict || !input?.confirmed_at || problemDirty || !writtenSteps(steps).length}>
                    풀이 확인하고 판정받기
                  </button>
                </div>
                <p className="solution-help">제출하면 입력한 풀이가 저장돼요. 틀린 줄을 고치거나 ‘지운 줄’로 표시한 뒤 다시 제출할 수 있어요.</p>
              </form>
              {!writtenSteps(steps).length && <button className="solution-button is-quiet" type="button" onClick={firstGuidance}
                disabled={disabled || conflict || !input?.confirmed_at || dirty}>풀이 전 시작 안내 보기</button>}
            </section>
          </div>

          <aside>
            <section className="solution-card" aria-labelledby="result-title" aria-live="polite">
              <p className="solution-eyebrow">03 · 확인하고 이어가기</p>
              <h2 id="result-title">풀이 확인</h2>
              {result && summary ? <div className={`solution-summary solution-notice ${summary.tone}`}>
                <h3>{summary.title}</h3>
                {verdict?.first_error_step_id && <p>{steps.findIndex((step) => step.id === verdict.first_error_step_id) + 1}번째 줄부터 다시 확인해 보세요.</p>}
                {!verdict?.first_error_step_id && verdict?.first_observed_error_step_id && <p>먼저 확인할 줄이 남아 있어 최초 오류 위치는 아직 확정하지 않았어요.</p>}
                <p>{result.evaluation.student_feedback.hint}</p>
                {verdict?.judgement === "incorrect" && verdict.completion === "complete" && <p>마지막 답이 맞아도 앞선 풀이 오류를 먼저 확인해 주세요.</p>}
              </div> : <p className="solution-empty">{dirty ? "작성한 내용을 제출하면 새 판정이 여기에 보여요." : "풀이를 제출하면 확인 결과와 다음 안내가 여기에 보여요."}</p>}
              {viewError && result && <div className="solution-notice is-warning">
                <p>판정은 저장됐지만 안내 확인 기록을 저장하지 못했어요.</p>
                <button className="solution-button is-quiet" type="button" onClick={() => void markViewed(result)}>안내 확인 기록 다시 저장</button>
              </div>}
            </section>

            <section className="solution-card" aria-labelledby="history-title">
              <h2 id="history-title">저장된 풀이 기록</h2>
              <p className="solution-help">최근 판정 10회를 보여드려요. 기록을 열어도 새 판정을 만들지 않아요.</p>
              {!snapshot.history.length && <p className="solution-empty">아직 저장된 판정이 없어요.</p>}
              <div className="solution-history">
                {snapshot.history.slice(0, 10).map((item) => <details className="solution-history-item" key={item.id}>
                  <summary>{dateLabel(item.created_at)} · {resultSummary(item).title}</summary>
                  <p className="solution-meta">당시 문제와 제출한 풀이</p>
                  <p className="solution-equation">{snapshot.inputs.find((entry) => entry.version_number === item.problem_version_number)?.content_text ?? "문제 내용은 입력 기록에서 확인해 주세요."}</p>
                  <ol>{item.evaluation.verification.steps.map((step) => <li key={step.step_id} className="solution-equation">
                    {step.text || "(표기 확인 필요)"}{step.skipped ? " · 지운 줄" : ""}
                  </li>)}</ol>
                  <p>{item.evaluation.student_feedback.hint}</p>
                </details>)}
              </div>
              {snapshot.views.length > 0 && <details className="solution-history-item">
                <summary>확인한 안내 {snapshot.views.length}개</summary>
                {[...snapshot.views].sort((a, b) => b.id - a.id).slice(0, 10).map((item) => <div key={item.id}>
                  <p className="solution-meta">{dateLabel(item.viewed_at)}</p><p>{item.feedback.hint}</p>
                </div>)}
              </details>}
            </section>
          </aside>
        </div>
      </>}
    </div>
  );
}
