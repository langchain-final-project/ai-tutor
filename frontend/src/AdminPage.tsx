import { useEffect, useState } from "react";
import { AdminInquiries, AdminMembers } from "./AdminMemberManagement";
import { ApiError, auth } from "./auth";
import logo from "./assets/didgo.svg";
import "./admin.css";

const groups = [
  {
    id: "members", label: "회원 관리", items: [
      { id: "inquiries", label: "문의" },
      { id: "member-edit", label: "회원 수정" },
    ],
  },
  {
    id: "problem-management", label: "문제 관리", items: [
      { id: "problems", label: "문제 목록" },
      { id: "problem-types", label: "유형·난이도 관리" },
    ],
  },
] as const;

const ocrCategory = { id: "ocr-failures", label: "OCR 검토" };

function selectedMenu() {
  const id = location.hash.slice(1);
  for (const group of groups) {
    const category = group.items.find((item) => item.id === id);
    if (category) return { category, groupId: group.id };
  }
  return { category: ocrCategory, groupId: null };
}

export function AdminPage({ loginUrl, homeUrl }: { loginUrl: string; homeUrl: string }) {
  const [selection, setSelection] = useState(selectedMenu);
  const [expanded, setExpanded] = useState<(typeof groups)[number]["id"] | null>(
    () => selection.groupId ?? groups[0].id,
  );
  const [session, setSession] = useState<"checking" | "admin" | "forbidden" | "unavailable">("checking");
  const [loggingOut, setLoggingOut] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    function updateCategory() {
      const next = selectedMenu();
      setSelection(next);
      if (next.groupId) setExpanded(next.groupId);
    }
    window.addEventListener("hashchange", updateCategory);
    return () => window.removeEventListener("hashchange", updateCategory);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    auth.adminSession(controller.signal).then(() => {
      if (!controller.signal.aborted) setSession("admin");
    }).catch((error: unknown) => {
      if (controller.signal.aborted) return;
      if (error instanceof ApiError && error.status === 401) {
        location.replace(loginUrl);
      } else {
        setSession(error instanceof ApiError && error.status === 403 ? "forbidden" : "unavailable");
      }
    });
    return () => controller.abort();
  }, [loginUrl]);

  if (session !== "admin") {
    return (
      <main className="admin-content">
        <h1>관리자</h1>
        {session === "checking" && <p role="status">관리자 권한을 확인하고 있어요.</p>}
        {session === "forbidden" && <p role="alert">관리자 권한이 필요합니다. <a href={homeUrl}>학습 홈으로</a></p>}
        {session === "unavailable" && <p role="alert">권한을 확인하지 못했어요. <button type="button" onClick={() => location.reload()}>다시 시도</button></p>}
      </main>
    );
  }

  async function logout() {
    setError("");
    setLoggingOut(true);
    try {
      await auth.logout();
      location.replace(loginUrl);
    } catch {
      setError("로그아웃하지 못했어요. 다시 시도해 주세요.");
      setLoggingOut(false);
    }
  }

  return (
    <div className="admin-page">
      <header className="admin-header">
        <img src={logo} alt="딛고" />
        <span>관리자</span>
        <a className="admin-home-link" href={homeUrl}>학습 홈</a>
      </header>
      <aside className="admin-sidebar">
        <nav aria-label="관리자 메뉴">
          <ul className="admin-menu">
            {groups.map((group) => (
              <li key={group.id}>
                <button className="admin-menu-toggle" type="button" aria-expanded={expanded === group.id}
                  aria-controls={`admin-menu-${group.id}`}
                  onClick={() => setExpanded((open) => open === group.id ? null : group.id)}>
                  {group.label}
                </button>
                <ul className="admin-submenu" id={`admin-menu-${group.id}`} hidden={expanded !== group.id}>
                  {group.items.map(({ id, label }) => (
                    <li key={id}>
                      <a href={`#${id}`} aria-current={selection.category.id === id ? "page" : undefined}>{label}</a>
                    </li>
                  ))}
                </ul>
              </li>
            ))}
            <li>
              <a href={`#${ocrCategory.id}`} aria-current={selection.category.id === ocrCategory.id ? "page" : undefined}>
                {ocrCategory.label}
              </a>
            </li>
          </ul>
        </nav>
        <div className="admin-sidebar-footer">
          {error && <p role="alert">{error}</p>}
          <button type="button" onClick={logout} disabled={loggingOut}>
            {loggingOut ? "로그아웃 중…" : "로그아웃"}
          </button>
        </div>
      </aside>
      <main className="admin-content" aria-labelledby="admin-title">
        <h1 id="admin-title">{selection.category.label}</h1>
        {selection.category.id === "inquiries" && <AdminInquiries />}
        {selection.category.id === "member-edit" && <AdminMembers />}
      </main>
    </div>
  );
}
