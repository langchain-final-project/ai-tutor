import { useState } from "react";
import type { FormEvent } from "react";
import { ApiError, auth } from "./auth";

const categories = [
  { id: "account", label: "계정" },
  { id: "learning", label: "학습" },
  { id: "other", label: "기타" },
];

export function AccountInquiry({ loginUrl, onClose }: { loginUrl: string; onClose: () => void }) {
  const [pending, setPending] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [error, setError] = useState("");

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pending) return;
    const values = new FormData(event.currentTarget);
    const category = String(values.get("category") ?? "").trim();
    const title = String(values.get("title") ?? "").trim();
    const body = String(values.get("body") ?? "").trim();
    if (!category || !title || !body) {
      setError("문의 유형, 제목, 내용을 모두 입력해 주세요.");
      return;
    }

    setError("");
    setPending(true);
    try {
      await auth.submitInquiry({ category, title, body });
      setSubmitted(true);
    } catch (error) {
      const status = error instanceof ApiError ? error.status : undefined;
      if (status === 401) {
        location.replace(loginUrl);
        return;
      }
      if (status === 429) {
        setError("문의 요청이 많아요. 잠시 후 다시 시도해 주세요.");
      } else if (status === 403) {
        setError("학생 계정에서 문의를 접수할 수 있어요.");
      } else if (status === 422) {
        setError("문의 유형, 제목, 내용을 확인해 주세요.");
      } else {
        setError("문의 접수 결과를 확인하지 못했어요. 잠시 후 다시 시도해 주세요.");
      }
    } finally {
      setPending(false);
    }
  }

  return (
    <section className="student-settings account-inquiry" aria-labelledby="inquiry-title">
      <h2 id="inquiry-title">문의하기</h2>
      {submitted ? (
        <>
          <p className="status" role="status">문의가 접수되었어요.</p>
          <button type="button" className="account-option" onClick={onClose}>설정으로 돌아가기</button>
        </>
      ) : (
        <form className="account-form" onSubmit={submit} aria-busy={pending}>
          <label className="field">
            <span>문의 유형</span>
            <select name="category" defaultValue="" required disabled={pending}>
              <option value="" disabled>문의 유형을 선택해 주세요</option>
              {categories.map(({ id, label }) => <option key={id} value={id}>{label}</option>)}
            </select>
          </label>
          <label className="field">
            <span>제목</span>
            <input name="title" required disabled={pending} />
          </label>
          <label className="field">
            <span>내용</span>
            <textarea name="body" rows={6} required disabled={pending} />
          </label>
          {error && <p className="error" role="alert">{error}</p>}
          <div className="account-actions">
            <button type="button" className="account-option" onClick={onClose} disabled={pending}>취소</button>
            <button type="submit" className="primary" disabled={pending}>{pending ? "접수 중…" : "문의 보내기"}</button>
          </div>
        </form>
      )}
    </section>
  );
}
