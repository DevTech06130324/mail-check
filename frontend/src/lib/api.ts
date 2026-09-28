import type { Account, Bootstrap, Settings, TriageData } from "./types"

export class ApiError extends Error {
  constructor(message: string, readonly status: number) { super(message) }
}

export async function request<T>(url: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: { ...(init.body ? { "Content-Type": "application/json" } : {}), ...init.headers },
  })
  const contentType = response.headers.get("content-type") || ""
  const value = contentType.includes("json") ? await response.json() : await response.text()
  if (!response.ok) {
    const message = typeof value === "string" ? value : value?.error || "Something went wrong."
    throw new ApiError(message, response.status)
  }
  return value as T
}

export const api = {
  bootstrap: () => request<Bootstrap>("/api/bootstrap"),
  triage: (params: URLSearchParams) => request<TriageData>(`/api/triage?${params}`),
  reader: (pk: number) => request<{ message: import("./types").Message; body_html: string }>(`/api/messages/${pk}`),
  accounts: () => request<{ accounts: Account[]; outlook_ready: boolean }>("/api/accounts"),
  settings: () => request<{ settings: Settings; rules: { category: string; sender: string | null; sender_domain: string | null; subject_contains: string | null }[]; categories: { name: string; label: string }[]; config_path: string; db_path: string }>("/api/settings"),
  dashboard: (params: URLSearchParams) => request<Record<string, any>>(`/api/dashboard?${params}`),
  status: () => request<{ running: boolean; completion_id: number; auto: boolean; next_in: number | null; message: string; detail: string; ok: boolean; stage_elapsed: number | null }>("/api/status"),
  post: <T,>(url: string, body?: unknown) => request<T>(url, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) }),
}

export function triageParams(search: string) {
  const source = new URLSearchParams(search)
  const params = new URLSearchParams()
  const view = source.get("view")
  params.set("view", view === "all" || view === "completed" ? view : "queue")
  const days = source.get("days")
  params.set("days", ["2", "3", "7", "14", "30", "90"].includes(days || "") ? days! : "30")
  for (const key of ["account", "category"]) if (source.get(key)) params.set(key, source.get(key)!)
  return params
}
