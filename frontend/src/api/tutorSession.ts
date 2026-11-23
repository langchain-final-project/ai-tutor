import { ApiError } from "../auth";
import type { Step } from "./solutionFlow";
import type { ConditionExtraction } from "../types/problemCondition";

export type TutorIntent = "hint" | "submit" | "more_help" | "answer";
export type TutorTurn = {
  request_id: string;
  expected_revision: number;
  intent: TutorIntent;
  student_text: string;
  steps: Step[];
};
export type TutorState = {
  attempts?: number;
  help_level?: number;
  hint_count?: number;
  answer_revealed?: boolean;
  assisted?: boolean;
  solved?: boolean;
  independent_verified?: boolean;
  needs_independent_check?: boolean;
  learning_status?: string;
};
export type TutorVerification = {
  judgement: "correct" | "incorrect" | "undetermined";
  action: string;
  completion: string;
  first_error_step_id?: string | null;
  earliest_unresolved_step_id?: string | null;
  steps?: { step_id: string; judgement: string; skipped?: boolean }[];
};
export type TypeMatch = {
  selected_type?: { source_topic_name: string; source_topic_code: string } | null;
  candidates?: { source_topic_name: string; source_topic_code: string }[];
  reasons?: string[];
  status?: string;
};
export type TutorResponse = {
  action: string;
  reason: string;
  policy_version: string;
  message: string;
  verification: TutorVerification | null;
  model: string | null;
  usage: Record<string, unknown>;
  output_verification?: { scope: string; status: string; prose_verified: false };
  output_guard?: string;
};
export type TutorEvent = {
  id: number;
  request_id: string;
  intent: TutorIntent;
  student_text: string;
  steps: Step[];
  response: TutorResponse;
  state_after: TutorState;
  created_at: string;
};
export type TutorTargetChoice = { id: string; label: string; display_text: string; target_text: string };
export type TutorTargetSelection = {
  status: "single_equation" | "selection_required" | "open_problem";
  normalized_text: string | null;
  choices: TutorTargetChoice[];
  message: string;
};
export type TutorTargetPreview = TutorTargetSelection & { submission_id: number; input_version_number: number };
export type TutorSnapshot = {
  id: number;
  submission_id: number;
  input_version_number: number;
  subject: string;
  attempt_mode?: "practice" | "independent" | "review";
  learning_context?: Record<string, unknown> | null;
  source_text: string;
  target_text: string;
  input_kind: "photo" | "text";
  type_match?: TypeMatch | null;
  condition_extraction?: ConditionExtraction | null;
  revision: number;
  is_current: boolean;
  can_submit?: boolean;
  target_selection?: TutorTargetSelection;
  created_at: string;
  state: TutorState;
  events: TutorEvent[];
  pending_request?: {
    request_id: string; status: string; error?: string | null;
    expected_revision?: number; intent?: TutorIntent; student_text?: string;
    steps?: Step[]; lease_expires_at?: number;
  } | null;
};

async function request<T>(path: string, signal?: AbortSignal, body?: unknown): Promise<T> {
  const response = await fetch(`/api${path}`, {
    signal, credentials: "same-origin",
    ...(body !== undefined && { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok) throw new ApiError(response.status, payload?.detail);
  if (payload === null) throw new Error("튜터 응답을 읽을 수 없어요. 저장된 기록을 확인해 주세요.");
  return payload as T;
}

export const tutorApi = {
  async list(signal?: AbortSignal): Promise<TutorSnapshot[]> {
    const value = await request<TutorSnapshot[] | { sessions: TutorSnapshot[] }>("/tutor-sessions", signal);
    return Array.isArray(value) ? value : value.sessions;
  },
  get: (id: number, signal?: AbortSignal) => request<TutorSnapshot>(`/tutor-sessions/${id}`, signal),
  targets: (submissionId: number, signal?: AbortSignal) => request<TutorTargetPreview>(`/submissions/${submissionId}/tutor-targets`, signal),
  start: (submissionId: number, version: number, target: string, signal?: AbortSignal) =>
    request<TutorSnapshot>(`/submissions/${submissionId}/tutor-sessions`, signal, {
      expected_input_version_number: version, target_text: target, subject: "mathematics",
    }),
  turn: (id: number, body: TutorTurn, signal?: AbortSignal) =>
    request<TutorSnapshot>(`/tutor-sessions/${id}/turns`, signal, body),
};

export function tutorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401) return "로그인이 필요해요.";
    if (error.status === 404) return "문제나 튜터 기록을 찾을 수 없어요. 내 기록에서 다시 선택해 주세요.";
    if (error.status === 409) return "문제 또는 튜터 상태가 바뀌었어요. 작성한 내용은 유지했으니 저장된 상태를 확인해 주세요.";
    if (error.status === 422) return typeof error.detail === "string" ? error.detail : "문제와 풀이 입력을 확인해 주세요.";
    if (error.status === 503 || error.status === 504) return "튜터의 응답을 받지 못했어요. 저장 상태를 확인한 뒤 같은 요청을 다시 시도할 수 있어요.";
  }
  return "연결이 끊겼어요. 작성한 내용과 요청을 남겨 두었으니 저장 상태를 확인해 주세요.";
}
