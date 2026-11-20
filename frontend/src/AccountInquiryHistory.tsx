import { useEffect, useState } from "react";
import { ApiError, auth } from "./auth";
import type { StudentInquiry, StudentInquiryDetail, StudentInquiryPage } from "./auth";
import chevron from "./assets/settings-chevron.svg";

const inquiryStatus = { pending: "답변 대기", answered: "답변 완료" };
const inquiryDate = new Intl.DateTimeFormat("ko-KR", { dateStyle: "medium" });

export function AccountInquiryHistory({ loginUrl, onClose }: { loginUrl: string; onClose: () => void }) {
  const [selected, setSelected] = useState<StudentInquiry | null>(null);
  const [cursors, setCursors] = useState<string[]>([]);
  const [result, setResult] = useState<StudentInquiryPage | StudentInquiryDetail | null>(null);
  const [revision, setRevision] = useState(0);
  const [error, setError] = useState("");
  const cursor = cursors.at(-1);
  const selectedId = selected?.inquiry_id;

  useEffect(() => {
    const controller = new AbortController();
    const request = selectedId ? auth.myInquiry(selectedId, controller.signal) : auth.myInquiries(cursor, controller.signal);
    request.then((data) => {
      if (!controller.signal.aborted) setResult(data);
    }).catch((error: unknown) => {
      if (controller.signal.aborted) return;
      const status = error instanceof ApiError ? error.status : undefined;
      if (status === 401) {
        location.replace(loginUrl);
        return;
      }
      if (status === 403) setError("학생 계정에서 내 문의를 확인할 수 있어요.");
      else if (status === 404) setError("문의를 찾을 수 없어요.");
      else setError("문의를 불러오지 못했어요.");
    });
    return () => controller.abort();
  }, [cursor, loginUrl, revision, selectedId]);

  function clearResult() {
    setResult(null);
    setError("");
  }

  function select(inquiry: StudentInquiry | null) {
    clearResult();
    setSelected(inquiry);
  }

  function movePage(nextCursors: string[]) {
    clearResult();
    setCursors(nextCursors);
  }

  const page = result && "items" in result ? result : null;
  const detail = result && "body" in result ? result : null;

  return (
    <section className="student-settings account-inquiry" aria-labelledby="inquiry-history-title">
      <h2 id="inquiry-history-title">내 문의</h2>
      {!result && !error && <p role="status">불러오는 중…</p>}
      {error && (
        <>
          <p className="error" role="alert">{error}</p>
          <button type="button" className="account-option" onClick={() => { clearResult(); setRevision(revision + 1); }}>
            다시 시도
          </button>
        </>
      )}
      {page && !page.items.length && <p className="account-description">아직 접수한 문의가 없어요.</p>}
      {page && page.items.length > 0 && (
        <ul className="account-card account-inquiry-list">
          {page.items.map((inquiry) => (
            <li key={inquiry.inquiry_id}>
              <button className="account-row" type="button" onClick={() => select(inquiry)}>
                <span className="account-inquiry-row">
                  <span>{inquiry.title}</span>
                  <span className="account-description">
                    <time dateTime={inquiry.created_at}>{inquiryDate.format(new Date(inquiry.created_at))}</time>
                    {" · "}{inquiryStatus[inquiry.status]}
                  </span>
                </span>
                <img src={chevron} alt="" />
              </button>
            </li>
          ))}
        </ul>
      )}
      {!selected && (cursors.length > 0 || page?.next_cursor) && (
        <nav className="account-inquiry-pagination" aria-label="내 문의 페이지">
          <button className="account-option" type="button" disabled={!cursors.length}
            onClick={() => movePage(cursors.slice(0, -1))}>이전</button>
          <span>{cursors.length + 1} 페이지</span>
          <button className="account-option" type="button" disabled={!page?.next_cursor}
            onClick={() => { if (page?.next_cursor) movePage([...cursors, page.next_cursor]); }}>다음</button>
        </nav>
      )}
      {detail && selected && (
        <article className="account-card account-inquiry-detail">
          <h3>{detail.title}</h3>
          <p className="account-description">
            <time dateTime={selected.created_at}>{inquiryDate.format(new Date(selected.created_at))}</time>
            {" · "}{inquiryStatus[detail.status]}
          </p>
          <p className="account-inquiry-text">{detail.body}</p>
          {detail.replies.map((reply) => (
            <section className="account-inquiry-reply" key={reply.reply_id}>
              <h4>관리자 답변</h4>
              <p className="account-inquiry-text">{reply.body}</p>
            </section>
          ))}
        </article>
      )}
      <button type="button" className="account-option" onClick={selected ? () => select(null) : onClose}>
        {selected ? "문의 목록으로" : "설정으로 돌아가기"}
      </button>
    </section>
  );
}
