import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { ApiError } from "./auth";
import { solutionApi } from "./api/solutionFlow";
import type { Step, Submission } from "./api/solutionFlow";
import { tutorApi, tutorMessage } from "./api/tutorSession";
import type { TutorEvent, TutorIntent, TutorSnapshot, TutorTurn, TypeMatch } from "./api/tutorSession";
import { MathFormula } from "./components/MathFormula";
import { ProblemConditions } from "./components/ProblemConditions";
import "./tutor-workspace.css";

type Props = { sessionId: string | null; sourceSubmissionId: string | null; loginUrl: string; userId: string };
type Draft = { steps: Step[]; studentText: string };
type Pending = { body: TutorTurn; startedAt: number };
type TypedSubmission = Submission & { type_match?: TypeMatch | null };
const newStep = (): Step => ({ id: crypto.randomUUID(), text: "", state: "active" });
const numericId = (value: string | null) => value && /^[1-9]\d*$/.test(value) && Number.isSafeInteger(Number(value)) ? Number(value) : null;
const intentLabel: Record<TutorIntent, string> = { hint: "첫 힌트", submit: "풀이 확인과 도움", more_help: "추가 도움", answer: "답·해설 보기" };

function readStored<T>(key: string): T | null {
  try { const value = sessionStorage.getItem(key); return value ? JSON.parse(value) as T : null; } catch { return null; }
}
function store(key: string, value: unknown) {
  try { if (value === null) sessionStorage.removeItem(key); else sessionStorage.setItem(key, JSON.stringify(value)); } catch { /* 저장 공간이 없어도 서버 기록은 유지된다. */ }
}
function timeLabel(value: string) {
  const date = new Date(value.includes("T") ? value : `${value.replace(" ", "T")}Z`);
  return Number.isNaN(date.getTime()) ? "저장된 기록" : date.toLocaleString("ko-KR", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
}
function TutorText({ text, generated = false }: { text: string; generated?: boolean }) {
  // 모델이 중복 이스케이프한 인라인 구분자만 표시할 때 정리하며 저장 원문은 보존한다.
  const display = generated ? text.replace(/\\{2,}([()])/g, "\\$1") : text;
  return <div className="tutor-message">{display.split(/(\$\$[\s\S]+?\$\$|\\\[[\s\S]+?\\\]|\\\([\s\S]+?\\\)|\$[^$\n]+\$)/g).map((part, index) => {
    const delimiter = part.startsWith("$$") || part.startsWith("\\[") || part.startsWith("\\(") ? 2 : part.startsWith("$") ? 1 : 0;
    return delimiter ? <MathFormula key={index} latex={part.slice(delimiter, -delimiter)} /> : <span key={index}>{part}</span>;
  })}</div>;
}
function TypeReference({ match }: { match?: TypeMatch | null }) {
  if (!match) return null;
  const names = match.selected_type ? [match.selected_type.source_topic_name] : match.candidates?.slice(0, 3).map((item) => item.source_topic_name) ?? [];
  return <div className="tutor-type-reference">
    <strong>학습 참고 유형</strong>
    <p>{names.length ? names.join(" · ") : "현재 연결된 단원에서 유형을 확인하지 못했어요."}</p>
    <p className="solution-help">유형은 도움을 위한 참고 정보예요. 풀이의 정오 판정과는 구분해요.</p>
  </div>;
}
function verdict(event: TutorEvent) {
  const value = event.response.verification;
  if (!value || value.action === "unsupported") return { label: "자동 판정 범위 밖 · 모델 안내", tone: "is-warning" };
  if (value.action === "ask_confirmation") return { label: "입력 확인 필요", tone: "is-warning" };
  if (value.completion === "no_work") return { label: "첫 풀이를 위한 도움", tone: "" };
  if (value.judgement === "incorrect") return { label: "다시 확인할 풀이가 있어요", tone: "is-error" };
  if (value.judgement === "correct" && value.completion === "complete") return { label: "지원 범위에서 정답 확인", tone: "is-success" };
  if (value.judgement === "correct") return { label: "여기까지 유효한 풀이예요", tone: "" };
  return { label: "판정 보류 · 확인이 필요해요", tone: "is-warning" };
}

export function TutorWorkspace({ sessionId, sourceSubmissionId, loginUrl, userId }: Props) {
  const id = numericId(sessionId);
  const sourceId = numericId(sourceSubmissionId);
  const storagePrefix = `didgo:tutor:${userId}:${id ?? "new"}`;
  const [snapshot, setSnapshot] = useState<TutorSnapshot | null>(null);
  const [sessions, setSessions] = useState<TutorSnapshot[]>([]);
  const [questions, setQuestions] = useState<Submission[]>([]);
  const [source, setSource] = useState<TypedSubmission | null>(null);
  const [question, setQuestion] = useState("");
  const [target, setTarget] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [steps, setSteps] = useState<Step[]>([newStep()]);
  const [studentText, setStudentText] = useState("");
  const [retry, setRetry] = useState<Pending | null>(null);
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [clock, setClock] = useState(Date.now());
  const [arrival, setArrival] = useState<number | null>(null);
  const alive = useRef(false);
  const busy = useRef(false);
  const controller = useRef<AbortController | null>(null);
  const hydrated = useRef(false);
  const responseAnchor = useRef<HTMLLIElement | null>(null);

  function fail(reason: unknown) {
    if (!alive.current || reason instanceof Error && reason.name === "AbortError") return;
    if (reason instanceof ApiError && reason.status === 401) { location.replace(loginUrl); return; }
    setError(tutorMessage(reason));
  }
  function apply(value: TutorSnapshot, keepDraft: boolean) {
    setSnapshot(value);
    let stored = readStored<Pending>(`${storagePrefix}:pending`);
    const serverPending = value.pending_request;
    const ignored = readStored<string>(`${storagePrefix}:ignored`);
    if (!stored && serverPending?.request_id !== ignored && serverPending?.intent && serverPending.expected_revision !== undefined && serverPending.status !== "stale") {
      stored = { body: {
        request_id: serverPending.request_id, expected_revision: serverPending.expected_revision,
        intent: serverPending.intent, student_text: serverPending.student_text ?? "", steps: serverPending.steps ?? [],
      }, startedAt: Date.now() };
      store(`${storagePrefix}:pending`, stored);
    }
    if (stored && value.events?.some((event) => event.request_id === stored.body.request_id)) {
      store(`${storagePrefix}:pending`, null); setRetry(null);
    } else if (stored && value.revision > stored.body.expected_revision && value.pending_request?.status !== "running") {
      // 다른 요청이 먼저 저장됐으면 입력은 남기고 오래된 요청 키만 해제한다.
      store(`${storagePrefix}:pending`, null); setRetry(null);
    } else setRetry(stored);
    if (!keepDraft) {
      const draft = readStored<Draft>(`${storagePrefix}:draft`);
      const last = [...(value.events ?? [])].reverse().find((event) => event.intent === "submit");
      setSteps(draft?.steps?.length ? draft.steps : last?.steps?.length ? last.steps : [newStep()]);
      setStudentText(draft?.studentText ?? "");
    }
  }
  async function load(keepDraft = true) {
    const signal = controller.current?.signal;
    try {
      if (sessionId !== null && id === null) throw new ApiError(404, null);
      if (id !== null) {
        const value = await tutorApi.get(id, signal);
        if (alive.current) apply(value, keepDraft);
      }
      else {
        const [saved, submissions] = await Promise.all([tutorApi.list(signal), solutionApi.list(signal)]);
        if (!alive.current) return;
        setSessions(saved); setQuestions(submissions);
        if (sourceSubmissionId !== null) {
          if (sourceId === null) throw new ApiError(404, null);
          const value = await solutionApi.get(sourceId, signal);
          if (!alive.current) return;
          setSource(value); setTarget(value.latest_input?.content_text ?? "");
        }
      }
    } catch (reason) { fail(reason); }
    finally { if (alive.current) { setLoading(false); hydrated.current = true; } }
  }
  useEffect(() => {
    alive.current = true; controller.current = new AbortController();
    void load(false);
    return () => { alive.current = false; controller.current?.abort(); };
  }, [id, sourceId]);
  useEffect(() => {
    if (id !== null && hydrated.current) store(`${storagePrefix}:draft`, { steps, studentText });
  }, [steps, studentText, id, storagePrefix]);
  useEffect(() => {
    if (!pending && !retry && snapshot?.pending_request?.status !== "running") return;
    const interval = window.setInterval(() => setClock(Date.now()), 200);
    return () => window.clearInterval(interval);
  }, [pending, retry, snapshot?.pending_request?.status]);
  useEffect(() => {
    if (id === null || snapshot?.pending_request?.status !== "running") return;
    // 복원 중에는 저장 상태만 읽으며 새 모델 요청을 만들지 않는다.
    const interval = window.setInterval(() => { void load(true); }, 3000);
    return () => window.clearInterval(interval);
  }, [id, snapshot?.pending_request?.status]);

  async function begin(event: FormEvent) {
    event.preventDefault();
    if (busy.current || !confirmed || !(target.trim() || question.trim())) return;
    busy.current = true; setPending("문제를 확인하고 튜터를 준비하고 있어요…"); setError("");
    try {
      const signal = controller.current?.signal;
      let current = source;
      if (!current) {
        current = await solutionApi.create(question.trim(), signal);
        if (alive.current) setSource(current);
      }
      if (!current.latest_input) throw new ApiError(409, null);
      if (!current.latest_input.confirmed_at) {
        if (current.input_kind === "photo") throw new ApiError(409, "사진을 먼저 확인해 주세요.");
        current = await solutionApi.confirmProblem(current.id, current.latest_input.content_text, current.latest_input.version_number, signal);
        if (alive.current) setSource(current);
      }
      const value = await tutorApi.start(current.id, current.latest_input!.version_number, target.trim() || question.trim(), signal);
      if (alive.current) location.hash = `#tutor/${value.id}`;
    } catch (reason) { fail(reason); }
    finally { busy.current = false; if (alive.current) setPending(""); }
  }
  async function chooseSource(value: string) {
    if (!value) { setSource(null); setTarget(""); setConfirmed(false); return; }
    setError(""); setConfirmed(false);
    try {
      const current = await solutionApi.get(Number(value), controller.current?.signal);
      if (alive.current) { setSource(current); setTarget(current.latest_input?.content_text ?? ""); }
    } catch (reason) { fail(reason); }
  }
  async function send(intent: TutorIntent, saved?: Pending) {
    if (busy.current || !snapshot || !snapshot.is_current) return;
    const active = steps.filter((step) => step.text.trim() || step.state === "uncertain");
    if (!saved && intent === "submit" && !active.length && !studentText.trim()) {
      setError("풀이 한 줄이나 생각한 내용을 적어 주세요. 풀이 없이 시작하려면 첫 힌트를 선택해 주세요."); return;
    }
    if (!saved && retry) { setError("이전 요청의 저장 상태를 먼저 확인하거나 같은 요청을 다시 시도해 주세요."); return; }
    const request: Pending = saved ? { ...saved, startedAt: Date.now() } : { body: {
      request_id: crypto.randomUUID(), expected_revision: snapshot.revision, intent,
      student_text: studentText.trim(), steps: active,
    }, startedAt: Date.now() };
    busy.current = true; setPending(`${intentLabel[request.body.intent]}을 준비하고 있어요…`); setError(""); setNotice("");
    setClock(Date.now()); setRetry(request); store(`${storagePrefix}:pending`, request);
    const started = performance.now();
    try {
      const value = await tutorApi.turn(snapshot.id, request.body, controller.current?.signal);
      if (!alive.current) return;
      apply(value, true);
      if (value.events.some((event) => event.request_id === request.body.request_id)) {
        setArrival((performance.now() - started) / 1000);
        setNotice("응답과 학습 상태를 저장했어요.");
        window.requestAnimationFrame(() => responseAnchor.current?.scrollIntoView({ behavior: "smooth", block: "center" }));
      } else setNotice("같은 요청이 처리 중이에요. 저장된 결과를 확인하고 있어요.");
    } catch (reason) { fail(reason); if (alive.current) void load(true); }
    finally { busy.current = false; if (alive.current) setPending(""); }
  }
  function updateStep(index: number, changes: Partial<Step>) {
    setSteps((current) => current.map((step, position) => position === index ? { ...step, ...changes } : step));
  }
  const disabled = loading || pending !== "";
  const running = snapshot?.pending_request?.status === "running" &&
    (snapshot.pending_request.lease_expires_at === undefined || snapshot.pending_request.lease_expires_at * 1000 > clock);
  const sourceUnconfirmed = source?.input_kind === "photo" && !source.latest_input?.confirmed_at;
  const last = snapshot?.events?.at(-1);
  const currentCheck = last?.intent === "submit" && JSON.stringify(last.steps) === JSON.stringify(steps.filter((step) => step.text.trim() || step.state === "uncertain")) ? last.response.verification : null;

  return <div className="solution-workspace tutor-workspace" aria-busy={disabled}>
    <div className="solution-section-header tutor-top-links">
      <a className="solution-inline-link" href="#tutor/new">{id ? "← 튜터 기록과 새 문제" : "AI 튜터"}</a>
      <a className="solution-inline-link" href="#history/solutions">기존 식 검증 열기 →</a>
    </div>
    {error && <div className="solution-notice is-error" role="alert"><p>{error}</p>
      <button className="solution-button is-secondary" type="button" disabled={disabled} onClick={() => void load(true)}>저장된 상태 확인</button>
    </div>}
    {notice && <p className="solution-notice" role="status">{notice}</p>}
    {loading && <p role="status">저장된 튜터 기록을 불러오고 있어요…</p>}
    {pending && <p className="solution-notice tutor-timing" role="status">{pending}{retry && ` ${(Math.max(0, clock - retry.startedAt) / 1000).toFixed(1)}초`}</p>}

    {!loading && id === null && <>
      <section className="solution-intro"><p className="solution-eyebrow">질문하고, 고치고, 다시 한 걸음</p>
        <h2>내 문제로 튜터와 시작해요</h2><p>확인한 문제와 내가 시도한 풀이에 맞춰 도움을 받아요.</p></section>
      <div className="solution-grid">
        <section className="solution-card">
          <h2>문제 확인</h2><a className="solution-button is-secondary" href="/review">사진으로 문제 가져오기</a>
          <form className="solution-form" onSubmit={begin}>
            {!sourceSubmissionId && <label className="solution-label">저장한 문제 선택
              <select className="solution-input" value={source?.id ?? ""} disabled={disabled} onChange={(event) => void chooseSource(event.target.value)}>
                <option value="">새 문제 직접 입력</option>
                {questions.map((item) => <option key={item.id} value={item.id}>{item.latest_input?.content_text.slice(0, 55) ?? item.original_filename ?? "사진 인식 대기"}</option>)}
              </select></label>}
            {source ? <>
              <p className="solution-help">저장된 원문</p><TutorText text={source.latest_input?.content_text ?? "사진 인식 내용을 먼저 확인해 주세요."} />
              {source.input_kind === "photo" && <a className="solution-inline-link" href={`/review?submission=${source.id}`}>사진·인식 내용 확인하기 →</a>}
              <TypeReference match={source.type_match} />
            </> : <label className="solution-label" htmlFor="tutor-question">문제의 본문·식
              <textarea id="tutor-question" className="solution-input" rows={4} value={question} maxLength={10000} required disabled={disabled}
                placeholder="예: 일차방정식 2(x+2)=3(x-1)을 풀어라." onChange={(event) => { setQuestion(event.target.value); setConfirmed(false); }} /></label>}
            <label className="solution-label" htmlFor="tutor-target">이번에 풀 부분 또는 식
              <textarea id="tutor-target" className="solution-input" rows={3} value={target} maxLength={10000} disabled={disabled}
                placeholder="한 문제 전체 또는 선택한 소문항·식을 적어 주세요." onChange={(event) => { setTarget(event.target.value); setConfirmed(false); }} /></label>
            <p className="solution-help">사진에 여러 문제가 있으면 이번에 풀 소문항을 명확히 적어 주세요. 식 검증을 받으려면 2(x+2)=3(x-1)처럼 평문 등식을 사용할 수 있어요.</p>
            <label className="tutor-confirm"><input type="checkbox" checked={confirmed} disabled={disabled || sourceUnconfirmed}
              onChange={(event) => setConfirmed(event.target.checked)} />문제 내용과 이번에 풀 대상을 확인했어요.</label>
            {sourceUnconfirmed && <p className="solution-notice is-warning">사진 확인 화면에서 인식한 문제를 먼저 확인해 주세요.</p>}
            <button className="solution-button" disabled={disabled || !confirmed || sourceUnconfirmed || !(target.trim() || question.trim())}>이 문제로 튜터 시작</button>
          </form>
        </section>
        <section className="solution-card"><h2>이어가는 튜터 기록</h2>
          <p className="solution-help">기록을 열면 저장된 도움을 복원해요. 새 힌트는 직접 요청할 때만 만들어요.</p>
          {!sessions.length && <p className="solution-empty">아직 튜터 기록이 없어요.</p>}
          <ul className="solution-list">{sessions.map((item) => <li key={item.id}><a className="solution-list-item" href={`#tutor/${item.id}`}>
            <span>{item.target_text}</span><span className="solution-meta">{timeLabel(item.created_at)} · {item.state?.solved ? "현재 문제 해결" : "학습 중"}{item.state?.answer_revealed ? " · 답·해설 공개됨" : ""}</span>
          </a></li>)}</ul>
        </section>
      </div>
    </>}

    {!loading && snapshot && <>
      {!snapshot.is_current && <div className="solution-notice is-warning">문제 내용이 바뀌어 이 기록은 이전 버전이에요. <a href={`#tutor/from-submission/${snapshot.submission_id}`}>현재 문제로 다시 시작하기</a></div>}
      <div className="tutor-layout">
        <div>
          <section className="solution-card"><p className="solution-eyebrow">01 · 확인한 문제</p><h2>이번에 풀 문제</h2><TutorText text={snapshot.target_text} />
            <TypeReference match={snapshot.type_match} />
            {snapshot.input_kind === "photo" && snapshot.condition_extraction && <details className="tutor-details"><summary>사진에서 읽은 조건 보기</summary>
              <p className="solution-help">사진에서 추출한 참고 정보예요. 원본과 다르면 문제 확인 화면에서 수정해 주세요.</p>
              <ProblemConditions extraction={snapshot.condition_extraction} /></details>}
            <details className="tutor-details"><summary>확인한 원문 보기</summary><TutorText text={snapshot.source_text} /></details>
            {snapshot.input_kind === "photo" && <details className="tutor-details"><summary>원본 사진 보기</summary>
              <img className="tutor-source-photo" src={`/api/submissions/${snapshot.submission_id}/photo?corrected=true&selected=true`} alt="문제로 사용한 사진" />
              <a href={`/review?submission=${snapshot.submission_id}`}>사진 확인 화면 열기</a></details>}
          </section>
          <section className="solution-card"><p className="solution-eyebrow">02 · 내 시도</p><h2>풀이와 생각을 적어 주세요</h2>
            <p className="solution-help">풀이를 아직 시작하지 않았다면 바로 첫 힌트를 요청해도 돼요. 수정한 줄은 다시 제출하면 새 기록으로 남아요.</p>
            <form className="solution-form" onSubmit={(event) => { event.preventDefault(); void send("submit"); }}>
              <ol className="solution-work-lines">{steps.map((step, index) => {
                const checked = currentCheck?.steps?.find((item) => item.step_id === step.id);
                return <li key={step.id} className={`solution-line ${checked?.judgement === "incorrect" ? "is-error" : ""} ${step.state === "uncertain" ? "is-unresolved" : ""} ${step.state === "deleted" ? "is-deleted" : ""}`}>
                  <span className="solution-line-number">{index + 1}</span><label className="solution-label solution-line-input">풀이 {index + 1}번째 줄
                    <input className="solution-input" value={step.text} maxLength={10000} disabled={disabled || !snapshot.is_current || !!retry}
                      onChange={(event) => updateStep(index, { text: event.target.value })} placeholder="내가 계산한 식 또는 풀이" /></label>
                  <label className="solution-label solution-line-state">줄 상태<select className="solution-input" value={step.state} disabled={disabled || !snapshot.is_current || !!retry}
                    onChange={(event) => updateStep(index, { state: event.target.value as Step["state"] })}>
                    <option value="active">사용</option><option value="uncertain">확인 필요</option><option value="deleted">지운 줄</option></select></label>
                </li>;
              })}</ol>
              <button className="solution-button is-secondary" type="button" disabled={disabled || !snapshot.is_current || !!retry || steps.length >= 100} onClick={() => setSteps((value) => [...value, newStep()])}>+ 풀이 줄 추가</button>
              <label className="solution-label" htmlFor="tutor-student-text">궁금한 점·내 생각<textarea id="tutor-student-text" className="solution-input" rows={3} maxLength={10000}
                value={studentText} disabled={disabled || !snapshot.is_current || !!retry} onChange={(event) => setStudentText(event.target.value)} placeholder="어디에서 막혔는지 자유롭게 적어 주세요." /></label>
              <p className="solution-help">도움을 요청하거나 제출하면 입력한 풀이를 확인한 것으로 기록해요. 읽기 어려운 줄은 ‘확인 필요’로 표시해 주세요.</p>
              <div className="tutor-actions">
                <button className="solution-button" disabled={disabled || !snapshot.is_current || !!retry}>풀이 제출하고 도움받기</button>
                <button className="solution-button is-secondary" type="button" disabled={disabled || !snapshot.is_current || !!retry} onClick={() => void send("hint")}>첫 힌트 보기</button>
                <button className="solution-button is-secondary" type="button" disabled={disabled || !snapshot.is_current || !!retry} onClick={() => void send("more_help")}>조금 더 도와주세요</button>
              </div>
              <details className="tutor-details"><summary>답과 전체 해설이 필요해요</summary><p className="solution-help">답을 보면 공개 이력이 남아요. 혼자 푸는 능력은 이후 다른 문제에서 따로 확인해요.</p>
                <button className="solution-button is-secondary" type="button" disabled={disabled || !snapshot.is_current || !!retry} onClick={() => void send("answer")}>답·해설 보기</button></details>
            </form>
          </section>
        </div>
        <aside>
          <section className="solution-card tutor-learning-state"><h2>이번 학습 상태</h2>
            <p>{snapshot.state.solved ? snapshot.state.assisted || snapshot.state.answer_revealed ? "도움을 받아 현재 문제를 해결했어요." : "현재 문제의 풀이를 확인했어요." : "현재 문제를 풀고 있어요."}</p>
            <p className="solution-help">{snapshot.state.independent_verified ? "독립 수행 확인됨" : "새로운 문제의 독립 수행은 아직 확인하지 않았어요."}</p>
            <dl><div><dt>풀이 시도</dt><dd>{snapshot.state.attempts ?? 0}회</dd></div><div><dt>공개한 힌트</dt><dd>{snapshot.state.hint_count ?? 0}회</dd></div></dl>
            {snapshot.state.answer_revealed && <p className="solution-notice is-warning">답·해설을 본 이력이 있어요.</p>}
            {arrival !== null && <p className="tutor-timing">이번 화면 응답 도착: {arrival.toFixed(1)}초</p>}
          </section>
          {retry && !pending && <section className="solution-card"><h2>{running ? "튜터가 처리 중이에요" : "이전 요청을 확인해 주세요"}</h2>
            <p className="solution-help">작성한 내용과 같은 요청을 보관했어요. 저장된 결과를 먼저 확인할 수 있어요.</p>
            <div className="tutor-actions"><button className="solution-button is-secondary" type="button" onClick={() => void load(true)}>저장 상태 확인</button>
              {!running && <button className="solution-button" type="button" onClick={() => void send(retry.body.intent, retry)}>같은 요청 다시 시도</button>}</div>
            {snapshot.pending_request?.status === "failed" && <button className="solution-button is-quiet" type="button" onClick={() => {
              store(`${storagePrefix}:ignored`, retry.body.request_id); store(`${storagePrefix}:pending`, null); setRetry(null);
              setError(""); setNotice("실패한 요청의 입력을 유지했어요. 수정한 뒤 새 요청으로 제출할 수 있어요.");
            }}>실패한 요청 대신 입력 수정하기</button>}
          </section>}
          <section className="solution-card"><h2>받은 도움과 시도</h2>
            {!snapshot.events?.length && <p className="solution-empty">직접 풀어보거나 첫 힌트를 요청해 보세요.</p>}
            <ol className="tutor-event-list">{(snapshot.events ?? []).map((event) => {
              const summary = verdict(event);
              return <li key={event.id} className="tutor-event" ref={event.id === last?.id ? responseAnchor : null} aria-live={event.id === last?.id ? "polite" : "off"}><div className="solution-meta">{timeLabel(event.created_at)} · {intentLabel[event.intent]}</div>
                {(event.student_text || event.steps?.length > 0) && <details className="tutor-details"><summary>이때 제출한 풀이·생각</summary>
                  {event.steps?.map((step, index) => <p key={step.id} className={step.state === "deleted" ? "tutor-deleted" : ""}>{index + 1}. {step.text || "읽기 확인 필요"}{step.state === "uncertain" ? " · 확인 필요" : step.state === "deleted" ? " · 지운 줄" : ""}</p>)}
                  {event.student_text && <p>{event.student_text}</p>}</details>}
                {event.response && <><p className={`tutor-verdict ${summary.tone}`}>{event.intent === "answer" ? "요청한 답·해설" : summary.label}</p>
                  <TutorText text={event.response.message} generated />
                  {event.response.model && <details className="tutor-details tutor-metrics"><summary>응답 정보</summary><p>모델: {event.response.model}</p>
                    {Object.entries(event.response.usage ?? {}).filter(([, value]) => typeof value === "number").map(([key, value]) => <p key={key}>{key}: {typeof value === "number" ? Number(value.toFixed(3)) : String(value)}</p>)}</details>}</>}
              </li>;
            })}</ol>
          </section>
        </aside>
      </div>
    </>}
  </div>;
}
