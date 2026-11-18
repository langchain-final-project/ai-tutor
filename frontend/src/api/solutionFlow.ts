import { ApiError } from "../auth";

export type Step = { id: string; text: string; state: "active" | "deleted" | "uncertain" };
export type ProblemInput = {
  id: number;
  submission_id: number;
  version_number: number;
  content_text: string;
  created_at: string;
  confirmed_at: string | null;
};
export type Submission = {
  id: number;
  owner_id: string;
  input_kind: "photo" | "text";
  created_at: string;
  latest_input: ProblemInput | null;
  review_status: "awaiting_input" | "needs_review" | "confirmed";
};
export type Work = {
  id: number;
  submission_id: number;
  problem_input_version_id: number;
  version_number: number;
  steps: Step[];
  created_at: string;
  confirmed_at: string | null;
};
type Judgement = "correct" | "incorrect" | "undetermined";
type CheckedStep = {
  step_id: string;
  text: string;
  state: Step["state"];
  judgement: Judgement;
  reason_code: string;
  skipped: boolean;
};
type StudentFeedback = { hint: string };
export type Verification = {
  id: number;
  submission_id: number;
  problem_version_number: number;
  work_version_number: number;
  input_fingerprint: string;
  created_at: string;
  is_current: boolean;
  evaluation: {
    verification: {
      judgement: Judgement;
      completion: "complete" | "incomplete" | "unknown" | "no_work";
      action: "continue" | "ask_confirmation" | "unsupported" | "first_hint";
      first_error_step_id: string | null;
      first_observed_error_step_id: string | null;
      earliest_unresolved_step_id: string | null;
      steps: CheckedStep[];
    };
    student_feedback: StudentFeedback;
  };
};
export type FeedbackView = {
  id: number;
  verification_result_id: number;
  feedback: StudentFeedback;
  viewed_at: string;
};

async function request<T>(path: string, signal?: AbortSignal, method = "GET", body?: unknown): Promise<T> {
  const response = await fetch(`/api${path}`, {
    method, credentials: "same-origin", signal,
    ...(body !== undefined && { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  });
  const payload = await response.json().catch(() => undefined);
  if (!response.ok) throw new ApiError(response.status, payload?.detail);
  if (payload === undefined) throw new Error("서버 응답을 읽을 수 없어요. 저장된 내용을 다시 확인해 주세요.");
  return payload as T;
}

const base = (id: number) => `/submissions/${id}`;
const versions = (problemVersion: number, workVersion: number) => ({
  expected_problem_version_number: problemVersion, expected_work_version_number: workVersion,
});

export const solutionApi = {
  list: (signal?: AbortSignal) => request<Submission[]>("/submissions?limit=100", signal),
  create: (contentText: string, signal?: AbortSignal) =>
    request<Submission>("/submissions/text", signal, "POST", { content_text: contentText }),
  get: (id: number, signal?: AbortSignal) => request<Submission>(base(id), signal),
  confirmProblem: (id: number, contentText: string, expectedVersion: number, signal?: AbortSignal) =>
    request<Submission>(`${base(id)}/confirm`, signal, "POST", {
      content_text: contentText, expected_version_number: expectedVersion,
    }),
  inputHistory: (id: number, signal?: AbortSignal) => request<ProblemInput[]>(`${base(id)}/input/history`, signal),
  getWork: (id: number, signal?: AbortSignal) => request<Work | null>(`${base(id)}/work`, signal),
  saveWork: (id: number, problemVersion: number, workVersion: number, steps: Step[], signal?: AbortSignal) =>
    request<Work>(`${base(id)}/work`, signal, "PUT", { ...versions(problemVersion, workVersion), steps }),
  confirmWork: (id: number, problemVersion: number, workVersion: number, signal?: AbortSignal) =>
    request<Work>(`${base(id)}/work/confirm`, signal, "POST", versions(problemVersion, workVersion)),
  verify: (id: number, problemVersion: number, workVersion: number, signal?: AbortSignal) =>
    request<Verification>(`${base(id)}/verify`, signal, "POST", versions(problemVersion, workVersion)),
  currentVerification: (id: number, signal?: AbortSignal) => request<Verification | null>(`${base(id)}/verification`, signal),
  verificationHistory: (id: number, signal?: AbortSignal) => request<Verification[]>(`${base(id)}/verification/history`, signal),
  feedbackViews: (id: number, signal?: AbortSignal) => request<FeedbackView[]>(`${base(id)}/feedback/history`, signal),
  recordFeedbackView: (id: number, resultId: number, signal?: AbortSignal) =>
    request<FeedbackView>(`${base(id)}/verification/${resultId}/feedback-viewed`, signal, "POST", {}),
};

export function solutionMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401) return "로그인이 필요해요. 다시 로그인해 주세요.";
    if (error.status === 404) return "문제를 찾을 수 없거나 접근할 수 없어요. 내 질문 내역에서 다시 선택해 주세요.";
    if (error.status === 409) return "저장된 내용이 달라졌어요. 최신 내용을 불러와 비교한 뒤 다시 제출해 주세요.";
    if (error.status === 422) return "문제의 식과 풀이 줄을 확인해 주세요. 각 식은 512자 이내로 입력할 수 있어요.";
    if (error.status === 503) return "저장 서버를 사용할 수 없어요. 잠시 후 저장된 내용을 확인해 주세요.";
  }
  return "서버 응답을 확인하지 못했어요. 작성한 내용을 남겨 두고 저장된 내용을 다시 확인해 주세요.";
}
