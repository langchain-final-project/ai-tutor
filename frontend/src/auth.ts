type Credentials = { email: string; password: string }
type Signup = Credentials & { name: string; password_confirmation: string }
type Login = Credentials & { remember_me: boolean }
type Account = { user_id: string }
export type Profile = Account & { name: string; email: string }

export class ApiError extends Error {
  constructor(public status: number, public detail: unknown) {
    super(`API responded with ${status}`)
  }
}

function send(path: string, options: RequestInit) {
  return fetch(`/api${path}`, { ...options, credentials: "same-origin" })
}

async function request<T>(path: string, body?: Login | Signup, signal?: AbortSignal) {
  const response = await send(path, {
    signal,
    ...(body && {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  })
  const payload = await response.json().catch(() => null)
  if (!response.ok || !payload) throw new ApiError(response.status, payload?.detail)
  return payload as T
}

export const auth = {
  signUp: (body: Signup) => request<Account>("/auth/signup", body),
  login: (body: Login) => request<Account>("/auth/login", body),
  async logout() {
    const response = await send("/auth/logout", { method: "POST" })
    if (!response.ok) throw new ApiError(response.status, null)
  },
  profile: (signal?: AbortSignal) => request<Profile>("/me", undefined, signal),
}

export function authMessage(error: unknown) {
  if (!(error instanceof ApiError)) return "서버에 연결할 수 없어요. 잠시 후 다시 시도해 주세요."
  if (error.status === 401) return "이메일 또는 비밀번호를 확인해 주세요."
  if (error.status === 409) return "이미 가입한 이메일이에요."
  if (error.status === 429) return "로그인 시도가 많아요. 잠시 후 다시 시도해 주세요."
  if (error.status === 422) {
    const minimum = typeof error.detail === "string"
      ? /^Password must be at least (\d+) characters$/.exec(error.detail)
      : null
    if (minimum) return `비밀번호를 ${minimum[1]}자 이상으로 설정해 주세요.`
    if (error.detail === "Passwords do not match") return "비밀번호와 비밀번호 확인이 일치하지 않아요."
    return "입력한 정보를 확인해 주세요."
  }
  return "서버에 연결할 수 없어요. 잠시 후 다시 시도해 주세요."
}
