import { useEffect, useState } from "react";
import { ApiError, auth } from "./auth";
import type { Profile } from "./auth";
import { AccountSettings } from "./AccountSettings";
import { SolutionWorkspace } from "./SolutionWorkspace";
import logo from "./assets/didgo.svg";
import "./home.css";
import "./solution-workspace.css";

const icons = {
  home: <><path d="m3 10 9-7 9 7v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" /><path d="M9 21v-8h6v8" /></>,
  practice: <><path d="m12 3 9 5-9 5-9-5z" /><path d="m3 12 9 5 9-5M3 16l9 5 9-5" /></>,
  history: <><path d="M3 12a9 9 0 1 1 2.6 6.4M3 5v7h7" /><path d="M12 7v5l3 2" /></>,
  settings: <><path d="m9 3-.6 2.2-2 .9-2.1-.8-2 3.4 1.6 1.5v2.4L2.3 14l2 3.5 2.1-.8 2 .9.6 2.4h4l.6-2.4 2-.9 2.1.8 2-3.5-1.6-1.4v-2.4l1.6-1.5-2-3.4-2.1.8-2-.9L13 3z" /><circle cx="11" cy="11.5" r="3" /></>,
  chevron: <path d="m9 6 6 6-6 6" />,
};

type Category = {
  id: Exclude<keyof typeof icons, "chevron">;
  label: string;
  title?: string;
  children?: readonly { id: string; label: string }[];
};

const categories: readonly Category[] = [
  { id: "home", label: "홈", title: "학습 홈" },
  {
    id: "practice", label: "단원 연습", children: [
      { id: "grades", label: "학년별 대단원" },
      { id: "concepts", label: "기본 개념" },
    ],
  },
  {
    id: "history", label: "학습 기록", children: [
      { id: "questions", label: "질문 내역" },
      { id: "solutions", label: "학습 풀이" },
      { id: "mistakes", label: "오답 노트" },
    ],
  },
  { id: "settings", label: "설정" },
];

function Icon({ name }: { name: keyof typeof icons }) {
  return (
    <svg className="student-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {icons[name]}
    </svg>
  );
}

function getSelection() {
  const [categoryId, itemId, submissionId] = location.hash.slice(1).split("/");
  const category = categories.find(({ id }) => id === categoryId) ?? categories[0];
  const item = category.children?.find(({ id }) => id === itemId);
  return {
    category,
    item,
    submissionId: category.id === "history" && (item?.id === "questions" || item?.id === "solutions")
      ? submissionId ?? null : null,
  };
}

export function HomePage({ loginUrl }: { loginUrl: string }) {
  const [selection, setSelection] = useState(getSelection);
  const [expanded, setExpanded] = useState<Category["id"] | null>(
    () => selection.category.children ? selection.category.id : null,
  );
  const [session, setSession] = useState<Profile | "checking" | "unavailable">("checking");

  useEffect(() => {
    const controller = new AbortController();
    auth.profile(controller.signal).then((profile) => {
      if (!controller.signal.aborted) setSession(profile);
    }).catch((error: unknown) => {
      if (controller.signal.aborted) return;
      if (error instanceof ApiError && error.status === 401) {
        location.replace(loginUrl);
      } else {
        setSession("unavailable");
      }
    });
    return () => controller.abort();
  }, [loginUrl]);

  useEffect(() => {
    function syncSelection() {
      const next = getSelection();
      setSelection(next);
      if (next.category.children) setExpanded(next.category.id);
    }
    window.addEventListener("hashchange", syncSelection);
    return () => window.removeEventListener("hashchange", syncSelection);
  }, []);

  const { category, item } = selection;
  const title = item?.label ?? category.title ?? category.label;
  const inSettings = category.id === "settings";
  const profile = typeof session === "object" ? session : null;
  const workspaceView = category.id === "home" ? "home"
    : category.id === "history" && (item?.id === "questions" || item?.id === "solutions") ? item.id : null;

  return (
    <div className="student-page">
      <header className="student-header">
        <a className="student-brand" href="#home" aria-label="딛고 학습 홈"><img src={logo} alt="" /></a>
        <span className="subject">중학 수학</span>
      </header>

      {profile && (
        <>
          <nav className="student-sidebar" aria-label="웹 주요 메뉴">
            <ul>
              {categories.map((entry) => (
                <li key={entry.id}>
                  {entry.children ? (
                    <>
                      <button className="student-menu-entry" type="button" aria-expanded={expanded === entry.id}
                        aria-controls={`student-menu-${entry.id}`}
                        onClick={() => setExpanded((open) => open === entry.id ? null : entry.id)}>
                        <Icon name={entry.id} />{entry.label}<span className="student-disclosure"><Icon name="chevron" /></span>
                      </button>
                      <ul className="student-submenu" id={`student-menu-${entry.id}`} hidden={expanded !== entry.id}>
                        {entry.children.map((child) => (
                          <li key={child.id}>
                            <a href={`#${entry.id}/${child.id}`} aria-current={category.id === entry.id && item?.id === child.id ? "page" : undefined}>
                              {child.label}
                            </a>
                          </li>
                        ))}
                      </ul>
                    </>
                  ) : (
                    <a className="student-menu-entry" href={`#${entry.id}`} aria-current={category.id === entry.id ? "page" : undefined}>
                      <Icon name={entry.id} />{entry.label}
                    </a>
                  )}
                </li>
              ))}
            </ul>
          </nav>
          <nav className="student-tabs" aria-label="앱 주요 메뉴">
            {categories.map((entry) => (
              <a key={entry.id} href={`#${entry.id}`} aria-current={category.id === entry.id ? "page" : undefined}>
                <Icon name={entry.id} />{entry.label}
              </a>
            ))}
          </nav>
        </>
      )}

      <main className={inSettings ? "student-content student-content--settings" : "student-content"} aria-labelledby="student-title">
        <h1 id="student-title">{title}</h1>
        {session === "checking" && <p role="status">로그인 상태를 확인하고 있어요.</p>}
        {session === "unavailable" && (
          <div className="student-settings">
            <p className="error" role="alert">로그인 상태를 확인할 수 없어요.</p>
            <button type="button" className="primary" onClick={() => location.reload()}>다시 시도</button>
          </div>
        )}
        {profile && (
          <>
            {!item && category.children && (
              <nav className="student-category-links" aria-label={`${category.label} 하위 메뉴`}>
                {category.children.map((child) => (
                  <a key={child.id} href={`#${category.id}/${child.id}`}>{child.label}<Icon name="chevron" /></a>
                ))}
              </nav>
            )}
            {workspaceView && (
              <SolutionWorkspace
                key={`${profile.user_id}:${workspaceView}:${selection.submissionId ?? "list"}`}
                view={workspaceView}
                submissionId={selection.submissionId}
                loginUrl={loginUrl}
              />
            )}
            {inSettings && <AccountSettings loginUrl={loginUrl} email={profile.email} />}
          </>
        )}
      </main>
    </div>
  );
}
