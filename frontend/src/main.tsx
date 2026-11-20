import { useEffect, useState } from "react";
import type { FormEvent, InputHTMLAttributes } from "react";
import { createRoot } from "react-dom/client";
import "@fontsource-variable/noto-sans-kr";
import { ApiError, auth, authMessage } from "./auth";
import type { Profile } from "./auth";
import { HomePage } from "./HomePage";
import logo from "./assets/didgo.svg";
import "./styles.css";

import { ProblemReviewPage } from "./ProblemReviewPage";

const loginUrl = "/login";
const signupUrl = "/signup";
const homeUrl = "/home";
const reviewUrl = "/review";
function afterLoginUrl() {
  const next = new URLSearchParams(location.search).get("next");
  return next && /^\/review(?:-preview)?(?:\?|$)/.test(next) ? next : homeUrl;
}
const slogan = "한 걸음씩, 내 힘으로";
const loginSteps = [
  ["문제에서 출발", "2x + 3 = 11", "궁금한 문제를 가져와요"],
  ["한 단계씩 이해", "2x = 8", "막힌 부분을 짚어봐요"],
  ["내 힘으로 해결", "x = 4", "스스로 답을 완성해요"],
];
const signupSteps = [
  ["내 문제에서 시작", "", "사진이나 수식으로 궁금한 문제를 가져와요."],
  ["막힌 부분을 이해", "", "필요한 단계에서 힌트를 받아요."],
  ["나의 힘으로 마무리", "", "직접 해결한 경험을 기록해요."],
];

function Introduction({ signup }: { signup: boolean }) {
  const steps = signup ? signupSteps : loginSteps;
  return (
    <aside className="introduction" aria-labelledby="introduction-title">
      <div>
        <span className="intro-badge">{signup ? slogan : "나의 속도로 배우는 수학"}</span>
        <h2 id="introduction-title">
          {signup ? <>오늘의 한 걸음,<br />딛고에서 시작해요.</> : <>막힌 수학을 딛고,<br />내 힘으로 한 걸음.</>}
        </h2>
        <p className="intro-copy">
          {signup ? <>정답을 외우기보다, 이해를 쌓는 공부.<br />나만의 학습 기록을 만들어 보세요.</> :
            <>틀린 지점은 함께 짚고, 다음 단계는 스스로.<br />정답을 넘어 이해하는 공부를 시작해요.</>}
        </p>
      </div>
      <ol className="steps" aria-label="학습 흐름">
        {steps.map(([title, example, description], index) => (
          <li key={title} className="step">
            <span className="step-number" aria-hidden="true">{String(index + 1).padStart(2, "0")}</span>
            <div className="step-copy">
              <strong>{title}</strong>
              {example && <span className="equation">{example}</span>}
              <p>{description}</p>
            </div>
          </li>
        ))}
      </ol>
      <p className="intro-caption">작은 이해가 쌓여, 스스로 푸는 힘이 돼요.</p>
      {!signup && (
        <dl className="features">
          <div><dt>내 문제 등록</dt><dd>사진 또는 수식 입력</dd></div>
          <div><dt>단계별 힌트</dt><dd>필요할 때 한 걸음씩</dd></div>
          <div><dt>학습 기록</dt><dd>쌓아온 이해를 확인</dd></div>
        </dl>
      )}
    </aside>
  );
}

function Field({ label, ...props }: InputHTMLAttributes<HTMLInputElement> & { label: string }) {
  return (
    <label className="field">
      <span>{label}</span>
      <input required {...props} />
    </label>
  );
}

function AuthPage({ signup }: { signup: boolean }) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const registered = !signup && new URLSearchParams(location.search).get("registered") === "1";
  const passwordChanged = !signup && new URLSearchParams(location.search).get("account") === "password-changed";
  const action = signup ? "회원가입" : "로그인";

  useEffect(() => {
    if (signup) return;
    const controller = new AbortController();
    auth.profile(controller.signal).then(() => {
      if (!controller.signal.aborted) location.replace(afterLoginUrl());
    }).catch(() => {
      // The login form is available even when there is no active session.
    });
    return () => controller.abort();
  }, [signup]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pending) return;
    const values = new FormData(event.currentTarget);
    const email = String(values.get("email")).trim();
    const password = String(values.get("password"));
    setError("");
    setNotice("");
    const confirmation = String(values.get("password_confirmation") ?? "");
    if (signup && password !== confirmation) {
      setError("비밀번호와 비밀번호 확인이 일치하지 않아요.");
      return;
    }

    setPending(true);
    try {
      if (signup) {
        await auth.signUp({ name: String(values.get("name")).trim(), email, password, password_confirmation: confirmation });
        location.assign(`${loginUrl}?registered=1`);
        return;
      }
      await auth.login({ email, password, remember_me: values.has("remember_me") });
      await auth.profile();
      location.replace(afterLoginUrl());
    } catch (error) {
      setError(!signup && error instanceof ApiError && error.status === 401 && error.detail === "Authentication required"
        ? "로그인 상태를 확인할 수 없어요. 브라우저의 쿠키 설정을 확인해 주세요."
        : authMessage(error));
    } finally {
      setPending(false);
    }
  }

  return (
    <div className={`auth-page ${signup ? "signup" : "login"}`}>
      <div className="desktop-strip"><span>학생 웹 · {action}</span><span>일차방정식 · 딛고</span></div>
      <header className="site-header">
        <a className="brand" href={loginUrl} aria-label="딛고 로그인">
          <span className="brand-tile"><img src={logo} alt="" /></span>
          <span className="desktop-only">{slogan}</span>
        </a>
        <div className="header-links">
          <a className="desktop-only" href="#introduction-title">서비스 소개</a>
          <span className="subject">중학 수학</span>
        </div>
      </header>
      <main className="auth-layout">
        <Introduction signup={signup} />
        <section className="auth-card" aria-labelledby="auth-title">
          {signup && <a className="back-link" href={loginUrl}>‹ 로그인으로</a>}
          <div className="welcome">
            <h1 id="auth-title">
              {signup ? "딛고와 첫걸음을 시작해요" :
                <><span className="mobile-only">다시 만나 반가워요</span><span className="desktop-only">딛고에 로그인</span></>}
            </h1>
            <p>{signup ? "계정을 만들고, 나만의 이해를 쌓아가요." :
              <><span className="mobile-only">로그인하고 나만의 학습을 이어가요.</span><span className="desktop-only">오늘의 한 걸음, 여기서 시작해요.</span></>}</p>
          </div>
          {!signup && (
            <div className="mobile-only learning-message">
              <strong>정답보다 중요한 건,<br />내가 이해한 한 단계.</strong>
              <div className="motif" aria-hidden="true"><i /><i /><i /></div>
            </div>
          )}
          {registered && <p className="status" role="status">회원가입이 완료됐어요. 가입한 계정으로 로그인해 주세요.</p>}
          {passwordChanged && <p className="status" role="status">비밀번호를 변경했어요. 새 비밀번호로 로그인해 주세요.</p>}
          <form className="auth-form" onSubmit={submit}>
            <div className="fields">
              {signup && <Field label="이름" name="name" placeholder="이름을 입력해 주세요" autoComplete="name" />}
              <Field label={signup ? "이메일" : "아이디(이메일)"} name="email" type="email" autoComplete="username"
                placeholder={signup ? "이메일 주소를 입력해 주세요" : "이메일을 입력해 주세요"} />
              <Field label="비밀번호" name="password" type="password" placeholder="비밀번호를 입력해 주세요"
                autoComplete={signup ? "new-password" : "current-password"} />
              {signup && <Field label="비밀번호 확인" name="password_confirmation" type="password"
                placeholder="비밀번호를 한 번 더 입력해 주세요" autoComplete="new-password" />}
            </div>
            {signup ? <p className="field-note">비밀번호와 비밀번호 확인에 같은 비밀번호를 입력해 주세요.</p> :
              <label className="remember"><input name="remember_me" type="checkbox" />로그인 상태 유지</label>}
            {error && <p className="error" role="alert">{error}</p>}
            <button className="primary" type="submit" disabled={pending}>{pending ? `${action} 중…` : action}</button>
          </form>
          {!signup && (
            <div className="recovery">
              <button type="button" onClick={() => setNotice("가입할 때 사용한 이메일로 로그인해 주세요.")}>아이디 찾기</button>
              <button type="button" onClick={() => setNotice("비밀번호 찾기는 준비 중이에요.")}>비밀번호 찾기</button>
              {notice && <p role="status">{notice}</p>}
            </div>
          )}
          <p className="switch-form">
            {signup ? "이미 딛고 계정이 있나요?" : "아직 딛고 계정이 없나요?"}
            <a href={signup ? loginUrl : signupUrl}>{signup ? "로그인" : "회원가입"}</a>
          </p>
          {!signup && <div className="desktop-only continue-note">
            <strong>어제의 이해를 오늘의 자신감으로.</strong>
            <p>지난 풀이와 학습 기록을 이어서 확인해요.</p>
          </div>}
        </section>
      </main>
      <footer className="site-footer">
        <span className="desktop-only">© {new Date().getFullYear()} 딛고. {slogan}.</span>
        <div className="footer-items">
          {!signup && <span>이용약관</span>}
          <span>개인정보처리방침</span>
          <span className="desktop-only">문의하기</span>
        </div>
        <span className="mobile-only">{slogan} · 딛고</span>
      </footer>
    </div>
  );
}

function ReviewRoute() {
  const [session, setSession] = useState<Profile | "checking" | "unavailable">("checking");
  useEffect(() => {
    const controller = new AbortController();
    auth.profile(controller.signal).then((profile) => {
      if (!controller.signal.aborted) setSession(profile);
    }).catch((error: unknown) => {
      if (controller.signal.aborted) return;
      if (error instanceof ApiError && error.status === 401) {
        location.replace(`${loginUrl}?next=${encodeURIComponent(location.pathname + location.search)}`);
      } else setSession("unavailable");
    });
    return () => controller.abort();
  }, []);
  if (session === "checking") return <p role="status">로그인 상태를 확인하고 있어요.</p>;
  if (session === "unavailable") return <div className="review-page">
    <p className="error" role="alert">로그인 상태를 확인할 수 없어요.</p>
    <button className="primary" onClick={() => location.reload()}>다시 시도</button>
  </div>;
  return <ProblemReviewPage key={session.user_id} onConfirmed={(submission) => {
    location.assign(`/home#history/solutions/from-photo/${submission.id}`);
  }} onTutor={(submission) => {
    location.assign(`/home#tutor/from-photo/${submission.id}`);
  }} />;
}

const isReview = location.pathname === reviewUrl ||
  (import.meta.env.DEV && location.pathname === "/review-preview");

createRoot(document.getElementById("root")!).render(
  isReview
    ? <ReviewRoute />
    : location.pathname === homeUrl
      ? <HomePage loginUrl={loginUrl} />
      : <AuthPage signup={location.pathname === signupUrl} />,
);
