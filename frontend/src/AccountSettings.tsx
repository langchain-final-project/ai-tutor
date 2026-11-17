import { useRef, useState } from "react";
import type { FormEvent } from "react";
import { ApiError, auth, authMessage } from "./auth";
import chevron from "./assets/settings-chevron.svg";
import dangerChevron from "./assets/settings-chevron-danger.svg";
import "./account-settings.css";

const actions = { password: "비밀번호 변경", withdrawal: "회원 탈퇴", logout: "로그아웃" };
type AccountAction = keyof typeof actions;

export function AccountSettings({ loginUrl, email }: { loginUrl: string; email: string }) {
  const [editing, setEditing] = useState<Exclude<AccountAction, "logout"> | null>(null);
  const [pending, setPending] = useState<AccountAction | null>(null);
  const [error, setError] = useState("");
  const accountForm = useRef<HTMLFormElement>(null);
  const withdrawalDialog = useRef<HTMLDialogElement>(null);

  function open(action: typeof editing) {
    setError("");
    setEditing(action);
  }

  async function submit(action: AccountAction, fields?: FormData) {
    if (pending) return;
    setError("");
    const currentPassword = String(fields?.get("current_password") ?? "");
    const newPassword = String(fields?.get("new_password") ?? "");
    const confirmation = String(fields?.get("password_confirmation") ?? "");

    if (action === "password" && newPassword !== confirmation) {
      setError("새 비밀번호와 비밀번호 확인이 일치하지 않아요.");
      return;
    }

    setPending(action);
    try {
      switch (action) {
        case "password":
          await auth.changePassword({ current_password: currentPassword, new_password: newPassword, password_confirmation: confirmation });
          location.replace(`${loginUrl}?account=password-changed`);
          break;
        case "withdrawal":
          await auth.withdraw(currentPassword);
          location.replace(loginUrl);
          break;
        case "logout":
          await auth.logout();
          location.replace(loginUrl);
          break;
      }
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        location.replace(loginUrl);
        return;
      }
      if (action === "withdrawal") withdrawalDialog.current?.close();
      if (error instanceof ApiError && error.status === 403) {
        setError("현재 비밀번호가 일치하지 않아요.");
      } else if (error instanceof ApiError && error.status === 429) {
        setError("비밀번호 확인 시도가 많아요. 잠시 후 다시 시도해 주세요.");
      } else if (error instanceof ApiError && error.status === 409 && action === "withdrawal") {
        setError("지금은 회원 탈퇴를 완료할 수 없어요. 잠시 후 다시 시도해 주세요.");
      } else {
        setError(action === "logout" ? "로그아웃하지 못했어요. 잠시 후 다시 시도해 주세요." : authMessage(error));
      }
      setPending(null);
    }
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pending) return;
    if (editing === "withdrawal") {
      setError("");
      withdrawalDialog.current?.showModal();
    } else if (editing === "password") {
      void submit(editing, new FormData(event.currentTarget));
    }
  }

  function confirmWithdrawal() {
    if (withdrawalDialog.current?.open && accountForm.current) {
      void submit("withdrawal", new FormData(accountForm.current));
    }
  }

  if (!editing) {
    return (
      <div className="account-settings">
        <dl className="account-card account-email">
          <dt>가입한 이메일</dt>
          <dd>{email}</dd>
        </dl>
        <div className="account-card account-menu">
          <button className="account-row" type="button" onClick={() => open("password")} disabled={pending !== null}>
            {actions.password}<img src={chevron} alt="" />
          </button>
          <button className="account-row" type="button" onClick={() => void submit("logout")} disabled={pending !== null}>
            {pending ? "로그아웃 중…" : actions.logout}<img src={chevron} alt="" />
          </button>
          <button className="account-row account-row--danger" type="button" onClick={() => open("withdrawal")} disabled={pending !== null}>
            {actions.withdrawal}<img src={dangerChevron} alt="" />
          </button>
        </div>
        {error && <p className="error" role="alert">{error}</p>}
      </div>
    );
  }

  const withdrawing = editing === "withdrawal";
  return (
    <section className="student-settings" aria-labelledby="account-action-title">
      <h2 id="account-action-title">{actions[editing]}</h2>
      <p className="account-description">
        {withdrawing ? "탈퇴하면 계정을 삭제하고 모든 기기에서 로그아웃해요." : "변경하면 모든 기기에서 로그아웃돼요."}
      </p>
      <form ref={accountForm} className="account-form" onSubmit={handleSubmit}>
        <label className="field">
          <span>현재 비밀번호</span>
          <input type="password" name="current_password" autoComplete="current-password" required autoFocus disabled={pending !== null} />
        </label>
        {!withdrawing && (
          <>
            <label className="field">
              <span>새 비밀번호</span>
              <input type="password" name="new_password" autoComplete="new-password" required disabled={pending !== null} />
            </label>
            <label className="field">
              <span>새 비밀번호 확인</span>
              <input type="password" name="password_confirmation" autoComplete="new-password" required disabled={pending !== null} />
            </label>
          </>
        )}
        {error && <p className="error" role="alert">{error}</p>}
        <div className="account-actions">
          <button className="account-option" type="button" onClick={() => open(null)} disabled={pending !== null}>취소</button>
          <button className="primary" type="submit" disabled={pending !== null}>
            {pending ? "처리 중…" : withdrawing ? "다음" : "비밀번호 저장"}
          </button>
        </div>
      </form>
      {withdrawing && (
        <dialog ref={withdrawalDialog} className="withdrawal-dialog" aria-labelledby="withdrawal-title"
          aria-describedby="withdrawal-description" onCancel={(event) => { if (pending) event.preventDefault(); }}>
          <h2 id="withdrawal-title">정말 회원 탈퇴를 하시겠어요?</h2>
          <p id="withdrawal-description" className="account-description">
            탈퇴하면 계정이 삭제되며, 삭제된 계정은 복구할 수 없어요.
          </p>
          <div className="account-actions">
            <button className="account-option" type="button" onClick={() => withdrawalDialog.current?.close()}
              disabled={pending !== null}>취소</button>
            <button className="primary account-danger" type="button" onClick={confirmWithdrawal} disabled={pending !== null}>
              {pending ? "처리 중…" : "탈퇴하기"}
            </button>
          </div>
        </dialog>
      )}
    </section>
  );
}
