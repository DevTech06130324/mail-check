export type Category = { name: string; label: string; tier: string; count?: number }
export type Message = {
  pk: number; message_id: string; subject: string; from_addr: string; from_name: string
  date_utc: string | null; snippet: string; provider_url: string | null; handled_at: string | null
  account_label: string; provider: string; category: string; category_label: string; tier: string
  confidence: number; company: string | null; role: string | null; deadline: string | null
  deadline_display: string; date_display: string; date_estimated: boolean; action_required: boolean
  summary: string; source: string; open_url: string | null; open_label: string
}
export type TriageGroup = { tier: string; label: string; items: Message[] }
export type TriageData = {
  view: "queue" | "all" | "completed"; groups: TriageGroup[]; total: number
  summary: Record<string, number>; counts: Record<string, number>
  accounts: { label: string; enabled: boolean }[]; days: number
  selected_account: string | null; selected_category: string | null
  last_run?: string
}
export type Account = {
  id: number; label: string; email: string; provider: string; imap_host: string
  imap_port: number; use_ssl: boolean; folder: string; enabled: boolean
}
export type Bootstrap = {
  readiness: { model: boolean; accounts: boolean; privacy_ack: boolean; outlook: boolean }
  counts: Record<string, number>; taxonomy: Category[]
  presets: { key: string; name: string; host: string; port: number; use_ssl: boolean; note: string; supported: boolean }[]
  last_run: string; account_count: number
}
export type Settings = {
  llm: { base_url: string; model: string; batch_size: number; max_body_chars: number; timeout_seconds: number; classification_deadline_seconds: number; concurrency: number; num_ctx: number; think: boolean; keep_alive: string }
  check: { lookback_days: number; retain_days: number }
  watch: { interval_minutes: number; auto_check: boolean }
  outlook: { client_id: string }; privacy_ack: boolean
}
export type ApiResult<T> = T
