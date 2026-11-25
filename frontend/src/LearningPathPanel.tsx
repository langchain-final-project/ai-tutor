import { useEffect, useRef, useState } from "react";
import { ApiError } from "./auth";
import { learningPathApi, learningPathMessage } from "./api/learningPath";
import type { LearningAvailability, LearningPath } from "./api/learningPath";
import type { TutorSnapshot } from "./api/tutorSession";

const phaseLabels: Record<string, string> = {
  remediation: "개념 보충", practice: "유사 문제 연습", independent: "독립 풀이", review_due: "복습 대기", review: "복습",
};
const outcomeLabels: Record<string, string> = {
  pending: "풀이 전", in_progress: "학습 중", incorrect: "다시 시도", undetermined: "판정 보류",
  practice_complete: "연습 풀이 확인", independent_pass: "이번 독립 풀이 확인", review_pass: "이번 복습 풀이 확인",
  assisted_complete: "도움을 받아 해결",
};
function dueLabel(value?: string | null) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date.toLocaleString("ko-KR", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
}
function needsLogin(error: unknown, loginUrl: string) {
  if (error instanceof ApiError && error.status === 401) { location.replace(loginUrl); return true; }
  return error instanceof Error && error.name === "AbortError";
}

export function LearningPathPanel({ snapshot, disabled, loginUrl }: { snapshot: TutorSnapshot; disabled: boolean; loginUrl: string }) {
  const [path, setPath] = useState<LearningPath | null>(null);
  const [availability, setAvailability] = useState<LearningAvailability | null>(null);
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [information, setInformation] = useState("");
  const [refresh, setRefresh] = useState(0);
  const busy = useRef(false);
  const mutation = useRef<AbortController | null>(null);
  const alive = useRef(false);

  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; mutation.current?.abort(); };
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError(""); setAvailability(null);
    learningPathApi.forSession(snapshot.id, controller.signal).then((value) => {
      if (!controller.signal.aborted) setPath(value);
    }).catch(async (reason) => {
      if (needsLogin(reason, loginUrl) || controller.signal.aborted) return;
      if (!(reason instanceof ApiError) || reason.status !== 404) {
        setError(learningPathMessage(reason));
        return;
      }
      setPath(null);
      try {
        const result = await learningPathApi.availability(snapshot.id, controller.signal);
        if (!controller.signal.aborted) setAvailability(result);
      } catch (availabilityError) {
        if (!controller.signal.aborted && !needsLogin(availabilityError, loginUrl)) {
          setError(learningPathMessage(availabilityError));
        }
      }
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [snapshot.id, snapshot.revision, refresh, loginUrl]);

  async function advance() {
    if (busy.current || disabled || !snapshot.is_current) return;
    busy.current = true; setPending(true); setError(""); setInformation("");
    const controller = new AbortController();
    mutation.current = controller;
    try {
      const value = path ? await learningPathApi.next(path.id, path.revision, controller.signal) : await learningPathApi.start(snapshot.id, controller.signal);
      if (!alive.current) return;
      setPath(value);
      if (value.active_session_id && value.active_session_id !== snapshot.id) location.hash = `#tutor/${value.active_session_id}`;
    } catch (reason) {
      if (!alive.current || needsLogin(reason, loginUrl)) return;
      if (reason instanceof ApiError && reason.status === 422) setInformation(learningPathMessage(reason));
      else setError(learningPathMessage(reason));
    } finally {
      busy.current = false;
      if (alive.current) setPending(false);
      if (mutation.current === controller) mutation.current = null;
    }
  }

  const eligible = snapshot.state.solved || snapshot.state.answer_revealed;
  const blocked = disabled || loading || pending || !snapshot.is_current;
  const due = dueLabel(path?.next_action.available_at);
  return <section className="solution-card tutor-learning-path" aria-busy={loading || pending}>
    <h2>다음 학습으로 이어가기</h2>
    <p className="solution-help">개념 보충 → 유사 문제 → 독립 풀이 → 복습을 작은 학습 경로로 이어가요.</p>
    {loading && <p role="status">저장된 학습 경로를 확인하고 있어요…</p>}
    {error && <div className="solution-notice is-error" role="alert"><p>{error}</p>
      <button type="button" className="solution-button is-secondary" disabled={pending} onClick={() => setRefresh((value) => value + 1)}>학습 경로 다시 확인</button></div>}
    {information && <p className="solution-notice" role="status">{information}</p>}
    {path ? <>
      <p><strong>{path.type.name}</strong> · {phaseLabels[path.phase] ?? "다음 학습 확인"}</p>
      {snapshot.type_match?.selected_type?.source_topic_code && snapshot.type_match.selected_type.source_topic_code !== path.type.code &&
        <p className="solution-help">이번에 선택한 식에 맞는 연습 유형으로 이어가요. 원문의 참고 유형과 다를 수 있어요.</p>}
      <p className="solution-help">{path.next_action.reason}</p>
      {path.remediation && <div className="tutor-remediation">
        <h3>{path.remediation.title}</h3><p>{path.remediation.text}</p>
        {path.remediation.concept_cards?.map((card) => <details key={card.concept_id} className="tutor-details">
          <summary>{card.title}</summary>
          <p>{card.definition}</p>
          {!!card.rules?.length && <ul>{card.rules.map((rule) => <li key={rule}>{rule}</li>)}</ul>}
        </details>)}
      </div>}
      {due && <p className="solution-help">복습 예정: {due}</p>}
      {path.source_session_id !== snapshot.id && <a className="solution-inline-link" href={`#tutor/${path.source_session_id}`}>처음 문제와 학습 경로 보기 →</a>}
      {path.active_session_id && path.active_session_id !== snapshot.id && <a className="solution-button is-secondary" href={`#tutor/${path.active_session_id}`}>진행 중인 학습 열기</a>}
      <button type="button" className="solution-button" disabled={blocked || !path.next_action.available} onClick={() => void advance()}>{pending ? "학습을 준비하고 있어요…" : path.next_action.label}</button>
      {!path.next_action.available && <button type="button" className="solution-button is-secondary" disabled={loading || pending} onClick={() => setRefresh((value) => value + 1)}>학습 경로 새로 확인</button>}
      {!!path.history.length && <details className="tutor-details"><summary>학습 경로와 풀이 이력</summary>
        <ol className="tutor-path-history">{path.history.map((item) => <li key={item.session_id}>
          <a href={`#tutor/${item.session_id}`}>{phaseLabels[item.mode] ?? "학습"} · {outcomeLabels[item.outcome.code] ?? "학습 결과 확인"}</a>
          <p>{item.question_text}</p>{item.selection_reason && <p className="solution-help">{item.selection_reason}</p>}
        </li>)}</ol></details>}
      <p className="solution-help">독립 풀이와 복습은 이번 문제에서의 수행 기록이에요. 개념 전체의 숙달 여부는 더 많은 학습으로 확인해요.</p>
    </> : !loading && availability && (availability.available ? <>
      {eligible ? <><p className="solution-help">현재 문제의 결과를 바탕으로 지원되는 유형의 다음 학습을 확인할 수 있어요.</p>
        <button type="button" className="solution-button" disabled={blocked} onClick={() => void advance()}>{pending ? "학습을 준비하고 있어요…" : "다음 학습 시작하기"}</button></> :
        <details className="tutor-details"><summary>막힌 개념부터 보충할래요</summary>
          <p className="solution-help">현재 문제가 어렵다면 지원되는 유형에서 개념을 보충하고 다른 유사 문제로 연습할 수 있어요.</p>
          <button type="button" className="solution-button is-secondary" disabled={blocked} onClick={() => void advance()}>{pending ? "학습을 준비하고 있어요…" : "보충 학습 시작하기"}</button>
        </details>}
    </> : <p className="solution-help" role="status">{availability.reason}</p>)}
  </section>;
}

export function LearningPathList({ loginUrl }: { loginUrl: string }) {
  const [paths, setPaths] = useState<LearningPath[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError("");
    learningPathApi.list(controller.signal).then((value) => { if (!controller.signal.aborted) setPaths(value); }).catch((reason) => {
      if (!needsLogin(reason, loginUrl) && !controller.signal.aborted) setError(learningPathMessage(reason));
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [refresh, loginUrl]);
  return <section className="solution-card tutor-learning-path"><h2>이어가는 학습 경로</h2>
    {loading && <p role="status">학습 경로를 불러오고 있어요…</p>}
    {error && <><p className="solution-help" role="status">{error}</p><button type="button" className="solution-button is-secondary" onClick={() => setRefresh((value) => value + 1)}>다시 확인</button></>}
    {!loading && !error && !paths.length && <p className="solution-empty">문제를 푼 뒤 유사 문제와 독립 풀이를 이어갈 수 있어요.</p>}
    <ul className="solution-list">{paths.map((path) => <li key={path.id}><a className="solution-list-item" href={`#tutor/${path.active_session_id ?? path.source_session_id}`}>
      <span>{path.type.name} · {phaseLabels[path.phase] ?? "학습 경로"}</span>
      <span className="solution-meta">{path.next_action.reason}{dueLabel(path.next_action.available_at) && ` · 복습 예정 ${dueLabel(path.next_action.available_at)}`}</span>
    </a></li>)}</ul>
  </section>;
}
