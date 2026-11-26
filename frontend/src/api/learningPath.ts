import { ApiError } from "../auth";

export type LearningAvailability = { available: boolean; reason: string; remediation_suggestion?: string };

export type LearningPath = {
  id: number;
  revision: number;
  source_session_id: number;
  type: { code: string; name: string };
  phase: string;
  status: string;
  next_action: { label: string; reason: string; available: boolean; available_at?: string | null };
  active_session_id: number | null;
  history: {
    session_id: number;
    mode: string;
    status: string;
    question_text: string;
    outcome: { code: string; assisted?: boolean; independent_verified?: boolean; verified_complete?: boolean };
    is_new_question?: boolean;
    selection_reason?: string;
  }[];
  remediation: {
    title: string;
    text: string;
    concept_cards: { concept_id: string; title: string; definition: string; rules?: string[] }[];
  } | null;
  mastery_claim: false;
};

async function request<T = LearningPath>(path: string, signal?: AbortSignal, body?: unknown): Promise<T> {
  const response = await fetch(`/api${path}`, {
    signal, credentials: "same-origin",
    ...(body !== undefined && { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok) throw new ApiError(response.status, payload?.detail);
  if (!payload) throw new Error("학습 경로를 읽을 수 없어요. 다시 확인해 주세요.");
  return payload as T;
}

export const learningPathApi = {
  async list(signal?: AbortSignal): Promise<LearningPath[]> {
    const response = await fetch("/api/learning-paths", { signal, credentials: "same-origin" });
    const payload = await response.json().catch(() => null);
    if (!response.ok) throw new ApiError(response.status, payload?.detail);
    if (!Array.isArray(payload)) throw new Error("학습 경로 목록을 읽을 수 없어요.");
    return payload as LearningPath[];
  },
  forSession: (sessionId: number, signal?: AbortSignal) => request(`/tutor-sessions/${sessionId}/learning-path`, signal),
  availability: (sessionId: number, signal?: AbortSignal) =>
    request<LearningAvailability>(`/tutor-sessions/${sessionId}/learning-availability`, signal),
  start: (sessionId: number, signal?: AbortSignal) => request(`/tutor-sessions/${sessionId}/learning-path`, signal, {}),
  get: (id: number, signal?: AbortSignal) => request(`/learning-paths/${id}`, signal),
  next: (id: number, revision: number, signal?: AbortSignal) => request(`/learning-paths/${id}/next`, signal, { expected_revision: revision }),
};

export function learningPathMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401) return "로그인이 필요해요.";
    if (error.status === 409) return "학습 경로가 변경됐어요. 현재 상태를 다시 확인해 주세요.";
    if (error.status === 422 && typeof error.detail === "string") return error.detail;
    if (error.status === 404) return "학습 경로를 찾을 수 없어요. 튜터 기록에서 다시 확인해 주세요.";
  }
  return "학습 경로를 불러오지 못했어요. 현재 풀이와 튜터 기록은 그대로 사용할 수 있어요.";
}
