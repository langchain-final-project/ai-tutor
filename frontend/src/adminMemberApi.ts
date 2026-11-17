import { ApiError, send } from "./auth";

export const inquiriesPath = "/admin/inquiries";
export const membersPath = "/admin/members";

export type Page<T> = { items: T[]; next_cursor: string | null };
export type Inquiry = { inquiry_id: string; title: string; status: "pending" | "answered" };
export type InquiryDetail = Inquiry & {
  body: string;
  replies: { reply_id: string; body: string }[];
};
export type Member = { member_id: string; name: string; email: string; status: "active" };
export type MemberDetail = Member & { version: number };

export async function adminRequest<T>(path: string, options: RequestInit) {
  const response = await send(path, options);
  if (!response.ok) throw new ApiError(response.status, null);
  return await response.json() as T;
}

export function saveMemberName(path: string, name: string, version: number) {
  return adminRequest<Pick<MemberDetail, "member_id" | "name" | "version">>(path, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, version }),
  });
}

export function sendInquiryReply(path: string, body: string, requestId: string) {
  return adminRequest<{ reply_id: string; status: "answered" }>(`${path}/replies`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ body, request_id: requestId }),
  });
}

export function adminError(error: unknown, fallback: string) {
  if (error instanceof ApiError) {
    if (error.status === 401) return "로그인 세션이 끝났어요. 다시 로그인해 주세요.";
    if (error.status === 403) return "관리자 권한이 필요합니다.";
    if (error.status === 404) return "항목이 삭제되었거나 더 이상 열 수 없어요.";
  }
  return fallback;
}
