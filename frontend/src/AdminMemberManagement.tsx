import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { ApiError } from "./auth";
import {
  adminError, adminRequest, inquiriesPath, membersPath, saveMemberName, sendInquiryReply,
} from "./adminMemberApi";
import type { Inquiry, InquiryDetail, Member, MemberDetail, Page } from "./adminMemberApi";
import "./admin-member-management.css";

function useAdminData<T>(path: string) {
  const [revision, setRevision] = useState(0);
  const key = `${path}:${revision}`;
  const [result, setResult] = useState<{ key: string; data?: T; error?: string }>();
  const current = result?.key === key ? result : undefined;

  useEffect(() => {
    const controller = new AbortController();
    adminRequest<T>(path, { signal: controller.signal }).then((data) => {
      if (!controller.signal.aborted) setResult({ key, data });
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) setResult({ key, error: adminError(error, "정보를 불러오지 못했어요.") });
    });
    return () => controller.abort();
  }, [path, key]);

  return { ...current, loading: !current, reload: () => setRevision((value) => value + 1) };
}

function useAdminList<T>(path: string, filter: Record<string, string>) {
  const [cursors, setCursors] = useState<string[]>([]);
  const [selected, select] = useState<string | null>(null);
  const query = new URLSearchParams(filter);
  const cursor = cursors.at(-1);
  if (cursor) query.set("cursor", cursor);
  const result = useAdminData<Page<T>>(`${path}?${query}`);

  return {
    ...result, selected, select, hasPrevious: cursors.length > 0, pageNumber: cursors.length + 1,
    previous: () => setCursors((current) => current.slice(0, -1)),
    next: () => {
      const nextCursor = result.data?.next_cursor;
      if (nextCursor) setCursors((current) => [...current, nextCursor]);
    },
    first: () => setCursors([]),
    reset: () => { setCursors([]); select(null); },
  };
}

function LoadState({ loading, error, retry }: { loading: boolean; error?: string; retry: () => void }) {
  if (loading) return <p role="status">불러오는 중…</p>;
  if (!error) return null;
  return <p role="alert">{error} <button type="button" onClick={retry}>다시 시도</button></p>;
}

const inquiryStatus = { pending: "답변 대기", answered: "답변 완료" };
const inquiryCategories: Record<string, string> = { learning: "학습", account: "계정", other: "기타" };

export function AdminInquiries() {
  const [status, setStatus] = useState("");
  const [category, setCategory] = useState("");
  const [sort, setSort] = useState("newest");
  const [notice, setNotice] = useState("");
  const filters: Record<string, string> = { sort };
  if (status) filters.status = status;
  if (category) filters.category = category;
  const list = useAdminList<Inquiry>(inquiriesPath, filters);

  function finishReply() {
    list.select(null);
    setNotice("답변을 등록했습니다.");
    list.reload();
  }

  return (
    <>
      <section className="admin-record-panel admin-member-list" aria-labelledby="admin-inquiry-list">
        <div className="admin-member-list-header admin-inquiry-list-header">
          <h2 id="admin-inquiry-list">문의 목록</h2>
          <div className="admin-inquiry-filters">
          <label className="admin-record-field">
            <span>답변 상태</span>
            <select value={status} onChange={(event) => { setStatus(event.target.value); list.reset(); setNotice(""); }}>
              <option value="">전체</option>
              {Object.entries(inquiryStatus).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select>
          </label>
          <label className="admin-record-field">
            <span>문의 유형</span>
            <select value={category} onChange={(event) => { setCategory(event.target.value); list.reset(); setNotice(""); }}>
              <option value="">전체</option>
              {Object.entries(inquiryCategories).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select>
          </label>
          <label className="admin-record-field">
            <span>정렬</span>
            <select value={sort} onChange={(event) => { setSort(event.target.value); list.reset(); setNotice(""); }}>
              <option value="newest">최신순</option>
              <option value="oldest">오래된순</option>
            </select>
          </label>
          </div>
        </div>
        {notice && <p role="status">{notice}</p>}
        <LoadState {...list} retry={list.reload} />
        {list.data && (
          <div className="admin-member-table-frame">
            <table className="admin-member-table" aria-labelledby="admin-inquiry-list">
              <thead><tr><th scope="col">문의 제목</th><th scope="col">문의 유형</th><th scope="col">답변 상태</th><th scope="col">작업</th></tr></thead>
              <tbody>
                {!list.data.items.length && <tr><td colSpan={4}>문의가 없습니다.</td></tr>}
                {list.data.items.map((inquiry) => (
                  <tr key={inquiry.inquiry_id}>
                    <th scope="row">{inquiry.title}</th>
                    <td>{inquiryCategories[inquiry.category] ?? inquiry.category}</td>
                    <td><span className="admin-member-status">{inquiryStatus[inquiry.status]}</span></td>
                    <td><button type="button" aria-haspopup="dialog"
                      aria-label={`${inquiry.title} ${inquiry.status === "pending" ? "답변 등록" : "답변 보기"}`}
                      onClick={() => { setNotice(""); list.select(inquiry.inquiry_id); }}>
                      {inquiry.status === "pending" ? "답변 등록" : "답변 보기"}
                    </button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <nav className="admin-record-pagination admin-member-pagination" aria-label="문의 목록 페이지">
          <button type="button" onClick={list.previous} disabled={!list.hasPrevious}>이전</button>
          <span aria-live="polite">{list.pageNumber} 페이지</span>
          <button type="button" onClick={list.next} disabled={!list.data?.next_cursor}>다음</button>
        </nav>
      </section>
      {list.selected && <InquiryPanel key={list.selected} id={list.selected}
        onReply={finishReply} onConflict={list.reload} onClose={() => list.select(null)} />}
    </>
  );
}

function InquiryPanel({ id, onReply, onConflict, onClose }: {
  id: string; onReply: () => void; onConflict: () => void; onClose: () => void;
}) {
  const path = `${inquiriesPath}/${encodeURIComponent(id)}`;
  const inquiry = useAdminData<InquiryDetail>(path);
  const request = useRef<{ body: string; id: string } | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const [body, setBody] = useState("");
  const [saving, setSaving] = useState(false);
  const submitting = useRef(false);
  const [error, setError] = useState("");

  useEffect(() => {
    const element = dialog.current;
    element?.showModal();
    return () => element?.close();
  }, []);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const answer = body.trim();
    if (submitting.current || !answer) return;
    submitting.current = true;
    setError("");
    setSaving(true);
    try {
      if (request.current?.body !== answer) request.current = { body: answer, id: crypto.randomUUID() };
      await sendInquiryReply(path, answer, request.current.id);
      onReply();
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        setError("이미 답변이 등록되었습니다. 현재 답변을 불러왔어요.");
        inquiry.reload();
        onConflict();
      } else {
        setError(adminError(error, "답변을 등록하지 못했어요. 다시 시도해 주세요."));
      }
    } finally {
      submitting.current = false;
      setSaving(false);
    }
  }

  return (
    <dialog ref={dialog} className="admin-inquiry-dialog" aria-labelledby="inquiry-dialog-title"
      onCancel={(event) => { event.preventDefault(); if (!submitting.current) onClose(); }}>
      <section className="admin-record-panel" aria-busy={saving}>
        <div className="admin-member-list-header">
          <h2 id="inquiry-dialog-title">{inquiry.data?.status === "answered" ? "답변 보기" : "문의 답변"}</h2>
          <button type="button" onClick={onClose} disabled={saving} autoFocus>닫기</button>
        </div>
        <LoadState {...inquiry} retry={inquiry.reload} />
        {error && <p role="alert">{error}</p>}
        {inquiry.data && (
          <>
            <h3>{inquiry.data.title}</h3>
            <p className="admin-record-message">{inquiry.data.body}</p>
            {inquiry.data.replies.map((reply) => (
              <div key={reply.reply_id}><h3>등록된 답변</h3><p className="admin-record-message">{reply.body}</p></div>
            ))}
            {inquiry.data.status === "pending" && (
              <form onSubmit={submit}>
                <label className="admin-record-field">
                  <span>답변</span>
                  <textarea required rows={6} value={body} disabled={saving} onChange={(event) => setBody(event.target.value)} />
                </label>
                <div className="admin-inquiry-actions">
                  <button type="button" onClick={onClose} disabled={saving}>취소</button>
                  <button className="admin-record-primary" type="submit" disabled={saving || !body.trim()}>
                    {saving ? "등록 중…" : "답변 등록"}
                  </button>
                </div>
              </form>
            )}
          </>
        )}
      </section>
    </dialog>
  );
}

export function AdminMembers() {
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState("newest");
  const list = useAdminList<Member>(membersPath, { q: query, sort });

  function submitSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setQuery(search.trim());
    list.reset();
    list.reload();
  }

  return (
    <>
      <div className="admin-record-layout">
        <section className="admin-record-panel" aria-labelledby="admin-member-search">
          <h2 id="admin-member-search">회원 검색</h2>
          <form className="admin-record-search" onSubmit={submitSearch}>
            <label className="admin-record-field">
              <span>이름 또는 이메일</span>
              <input type="search" value={search} onChange={(event) => setSearch(event.target.value)} />
            </label>
            <button type="submit">검색</button>
          </form>
        </section>
        {list.selected && <MemberPanel key={list.selected} id={list.selected} onSave={list.reload} />}
      </div>

      <section className="admin-record-panel admin-member-list" aria-labelledby="admin-member-list">
        <div className="admin-member-list-header">
          <h2 id="admin-member-list">회원 목록</h2>
          <label className="admin-record-field">
            <select aria-label="회원 목록 정렬" value={sort} onChange={(event) => {
              setSort(event.target.value);
              list.first();
            }}>
              <option value="newest">최근순</option>
              <option value="oldest">오래된순</option>
            </select>
          </label>
        </div>
        <LoadState {...list} retry={list.reload} />
        {list.data && (
          <div className="admin-member-table-frame">
            <table className="admin-member-table" aria-labelledby="admin-member-list">
              <thead>
                <tr>
                  <th scope="col">회원 ID</th>
                  <th scope="col">이름</th>
                  <th scope="col">이메일</th>
                  <th scope="col">상태</th>
                  <th scope="col">작업</th>
                </tr>
              </thead>
              <tbody>
                {!list.data.items.length && <tr><td colSpan={5}>회원을 찾을 수 없습니다.</td></tr>}
                {list.data.items.map((member) => (
                  <tr key={member.member_id} className={list.selected === member.member_id ? "is-selected" : undefined}>
                    <th scope="row">{member.member_id}</th>
                    <td>{member.name}</td>
                    <td>{member.email}</td>
                    <td><span className="admin-member-status">이용 중</span></td>
                    <td>
                      <button type="button" aria-label={`${member.name} (${member.email}) 수정`}
                        onClick={() => list.select(member.member_id)}>수정</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <nav className="admin-record-pagination admin-member-pagination" aria-label="회원 목록 페이지">
          <button type="button" onClick={list.previous} disabled={!list.hasPrevious}>이전</button>
          <span aria-live="polite">{list.pageNumber} 페이지</span>
          <button type="button" onClick={list.next} disabled={!list.data?.next_cursor}>다음</button>
        </nav>
      </section>
    </>
  );
}

function MemberPanel({ id, onSave }: { id: string; onSave: () => void }) {
  const path = `${membersPath}/${encodeURIComponent(id)}`;
  const member = useAdminData<MemberDetail>(path);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const panel = useRef<HTMLElement>(null);

  useEffect(() => { panel.current?.focus(); }, []);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!member.data || saving) return;
    const name = String(new FormData(event.currentTarget).get("name")).trim();
    if (!name) return;
    setError("");
    setNotice("");
    setSaving(true);
    try {
      await saveMemberName(path, name, member.data.version);
      setNotice("회원 이름을 저장했습니다.");
      member.reload();
      onSave();
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        setError("다른 변경이 먼저 저장되었습니다. 최신 이름을 확인해 주세요.");
        member.reload();
        onSave();
      } else {
        setError(adminError(error, "이름을 저장하지 못했어요. 다시 시도해 주세요."));
      }
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="admin-record-panel" aria-label="회원 상세" ref={panel} tabIndex={-1}>
      <h2>회원 정보</h2>
      <LoadState {...member} retry={member.reload} />
      {notice && <p role="status">{notice}</p>}
      {error && <p role="alert">{error}</p>}
      {member.data && (
        <form onSubmit={submit}>
          <label className="admin-record-field"><span>이메일</span><input value={member.data.email} readOnly /></label>
          <label className="admin-record-field"><span>이름</span><input name="name" defaultValue={member.data.name} required /></label>
          <button className="admin-record-primary" type="submit" disabled={saving}>{saving ? "저장 중…" : "이름 저장"}</button>
        </form>
      )}
    </section>
  );
}
