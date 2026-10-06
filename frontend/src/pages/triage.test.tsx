import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { ToastProvider } from "@/components/common"
import type { Bootstrap, Message } from "@/lib/types"
import TriagePage, { Reader } from "./triage"

const messages: Message[] = [1, 2].map((pk) => ({
  pk, message_id: `${pk}@example.test`, subject: `Interview ${pk}`, from_addr: "recruiter@example.test",
  from_name: "Recruiter", date_utc: null, snippet: "", provider_url: null, handled_at: null,
  account_label: "Personal", provider: "imap", category: "interview_invite", category_label: "Interview invite",
  tier: "act", confidence: .9, company: "Acme", role: "Engineer", deadline: null,
  deadline_display: "", date_display: "Today", date_estimated: false, action_required: true,
  summary: `Summary ${pk}`, source: "llm", open_url: null, open_label: "",
}))
const bootstrap = {
  readiness: { model: true, accounts: true, privacy_ack: true, outlook: false },
  counts: { actionable: 2, informational: 0, done: 0 }, taxonomy: [], presets: [], last_run: "Never checked", account_count: 1,
} as Bootstrap

let done = new Set<number>()
let failDone = false
let slowDone = false
let finishDone: (() => void) | null = null
let requests: string[] = []
let unclassified = 0
let jobStatus = { running: false, message: "" }

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } }))
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={queryClient}><ToastProvider><TriagePage bootstrap={bootstrap} /></ToastProvider></QueryClientProvider>)
}

function renderWithReader(message: Message) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={queryClient}><ToastProvider><Reader message={message} mobile onBack={() => undefined} onDone={() => undefined} busy={false} canRetry /></ToastProvider></QueryClientProvider>)
}

function setCompactViewport(matches: boolean) {
  Object.defineProperty(window, "matchMedia", { configurable: true, value: vi.fn(() => ({ matches })) })
}

beforeEach(() => {
  done = new Set()
  failDone = false
  slowDone = false
  finishDone = null
  requests = []
  unclassified = 0
  jobStatus = { running: false, message: "" }
  window.history.replaceState({}, "", "/")
  setCompactViewport(false)
  HTMLElement.prototype.scrollIntoView = vi.fn()
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    requests.push(`${init?.method || "GET"} ${url} ${String(init?.body || "")}`)
    if (url.startsWith("/api/triage")) {
      const visible = messages.filter((message) => !done.has(message.pk))
      return response({ view: "queue", groups: visible.length ? [{ tier: "act", label: "Act now", items: visible }] : [],
        total: visible.length, summary: { actionable: visible.length, informational: 0, done: done.size },
        counts: { interview_invite: visible.length, unclassified }, accounts: [{ label: "Personal", enabled: true }],
        days: 30, selected_account: null, selected_category: null })
    }
    if (url.startsWith("/api/messages/") && init?.method === "POST") {
      if (failDone) return response({ error: "Server rejected the change" }, 500)
      const pk = Number(url.match(/messages\/(\d+)/)?.[1])
      if (slowDone) return new Promise((resolve) => { finishDone = () => { done.add(pk); resolve(response({ ok: true, message: "Updated", summary: {} })) } })
      if (url.includes("/handled?done=true")) done.add(pk)
      return response({ ok: true, message: "Updated", summary: {} })
    }
    if (url.startsWith("/api/messages/undo-last")) return response({ message: "Restored." })
    if (url === "/api/reclassify") return response({ ok: true, message: "Started." })
    if (url === "/api/status") return response({ ...jobStatus, completion_id: 1, auto: false, next_in: null, detail: "", ok: true, stage_elapsed: null })
    if (url.startsWith("/api/messages/")) {
      const pk = Number(url.split("/").pop())
      const message = messages.find((item) => item.pk === pk)
      return response({ message, body_html: `<p>Body for ${pk}</p>` })
    }
    return response({ error: "Not found" }, 404)
  }))
})

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe("triage interactions", () => {
  it("keeps rendering after completing the final message in the queue", async () => {
    renderPage()
    const first = await screen.findByRole("option", { name: /Summary 1/ })
    first.focus()
    fireEvent.keyDown(first, { key: "e" })
    await waitFor(() => expect(screen.queryByRole("option", { name: /Summary 1/ })).toBeNull())
    const last = screen.getByRole("option", { name: /Summary 2/ })
    last.focus()
    fireEvent.keyDown(last, { key: "e" })
    expect(await screen.findByText("Your queue is clear")).toBeTruthy()
    expect(screen.queryByRole("option")).toBeNull()
  })

  it("loads the selected reader and follows arrow-key focus", async () => {
    renderPage()
    expect(await screen.findByText("Interview 1")).toBeTruthy()
    expect(await screen.findByText("Body for 1")).toBeTruthy()
    const page = document.querySelector(".triage-page")!
    fireEvent.keyDown(page, { key: "ArrowDown" })
    await waitFor(() => expect(document.activeElement).toBe(document.querySelector('[data-pk="2"]')))
    expect(await screen.findByText("Body for 2")).toBeTruthy()
  })

  it("advances the selection after a successful Done", async () => {
    renderPage()
    expect(await screen.findByText("Body for 1")).toBeTruthy()
    fireEvent.click(screen.getByRole("button", { name: "Done" }))
    await waitFor(() => expect(document.querySelector('[data-pk="2"]')?.getAttribute("aria-selected")).toBe("true"))
    await waitFor(() => expect(document.activeElement).toBe(document.querySelector('[data-pk="2"]')))
    expect(requests.some((request) => request.includes("POST /api/messages/1/handled?done=true"))).toBe(true)
  })

  it("moves keyboard focus to the next message after E completes the current one", async () => {
    renderPage()
    const current = await screen.findByRole("option", { name: /Summary 1/ })
    current.focus()
    fireEvent.keyDown(current, { key: "e" })
    await waitFor(() => expect(document.querySelector('[data-pk="2"]')?.getAttribute("aria-selected")).toBe("true"))
    await waitFor(() => expect(document.activeElement).toBe(document.querySelector('[data-pk="2"]')))
    fireEvent.keyDown(document.activeElement!, { key: "e" })
    await waitFor(() => expect(requests.some((request) => request.includes("POST /api/messages/2/handled?done=true"))).toBe(true))
  })

  it("returns focus to the restored message after a failed Done", async () => {
    renderPage()
    const current = await screen.findByRole("option", { name: /Summary 1/ })
    current.focus()
    failDone = true
    fireEvent.keyDown(current, { key: "e" })
    await waitFor(() => expect(document.activeElement).toBe(document.querySelector('[data-pk="1"]')))
  })

  it("ignores a second E while the first Done request is still pending", async () => {
    renderPage()
    const current = await screen.findByRole("option", { name: /Summary 1/ })
    current.focus()
    slowDone = true
    fireEvent.keyDown(current, { key: "e" })
    await waitFor(() => expect(requests.filter((request) => request.includes("POST /api/messages/")).length).toBe(1))
    fireEvent.keyDown(current, { key: "e" })
    expect(requests.filter((request) => request.includes("POST /api/messages/")).length).toBe(1)
    await act(async () => { finishDone?.(); await Promise.resolve() })
    await waitFor(() => expect(document.activeElement).toBe(document.querySelector('[data-pk="2"]')))
  })

  it("returns focus to the selected row when leaving the mobile reader", async () => {
    setCompactViewport(true)
    renderPage()
    fireEvent.click(await screen.findByRole("option", { name: /Summary 1/ }))
    expect(await screen.findByText("Body for 1")).toBeTruthy()
    fireEvent.click(screen.getByRole("button", { name: "All messages" }))
    await waitFor(() => expect(document.activeElement).toBe(document.querySelector('[data-pk="1"]')))
  })

  it("keeps keyboard focus in the mobile reader while advancing after Done", async () => {
    setCompactViewport(true)
    renderPage()
    fireEvent.click(await screen.findByRole("option", { name: /Summary 1/ }))
    expect(await screen.findByText("Body for 1")).toBeTruthy()
    await waitFor(() => expect(document.activeElement).toBe(document.querySelector(".reader-pane")))
    fireEvent.keyDown(document.activeElement!, { key: "e" })
    await waitFor(() => expect(requests.some((request) => request.includes("POST /api/messages/1/handled?done=true"))).toBe(true))
    expect(await screen.findByText("Body for 2")).toBeTruthy()
    await waitFor(() => expect(document.activeElement).toBe(document.querySelector(".reader-pane")))
    fireEvent.keyDown(document.activeElement!, { key: "e" })
    await waitFor(() => expect(requests.some((request) => request.includes("POST /api/messages/2/handled?done=true"))).toBe(true))
  })

  it("advances after Done and restores selection when the mutation fails", async () => {
    renderPage()
    expect(await screen.findByText("Body for 1")).toBeTruthy()
    failDone = true
    fireEvent.click(screen.getByRole("button", { name: "Done" }))
    await waitFor(() => expect(document.querySelector('[data-pk="1"]')?.getAttribute("aria-selected")).toBe("true"))
    expect(requests.some((request) => request.includes("POST /api/messages/1/handled?done=true"))).toBe(true)
    expect(await screen.findByText("Server rejected the change")).toBeTruthy()
  })

  it("uses the undo shortcut and does not steal undo from an input", async () => {
    renderPage()
    await screen.findByText("Interview 1")
    const input = document.createElement("input")
    document.body.appendChild(input)
    fireEvent.keyDown(input, { key: "z", ctrlKey: true })
    input.remove()
    expect(requests.some((request) => request.includes("/api/messages/undo-last"))).toBe(false)
    fireEvent.keyDown(document, { key: "z", ctrlKey: true })
    await waitFor(() => expect(requests.some((request) => request.includes("/api/messages/undo-last"))).toBe(true))
  })

  it("retries one unclassified message from the shared reader", async () => {
    const message = { ...messages[0], category: "unclassified", category_label: "Needs a manual look" }
    renderWithReader(message)
    fireEvent.click(await screen.findByRole("button", { name: "Try classification again" }))
    await waitFor(() => expect(requests.some((request) => request.includes('POST /api/reclassify {"pks":[1]}'))).toBe(true))
  })
})

describe("triage chrome", () => {
  it("counts active filters on the Filters button even when the panel is closed", async () => {
    window.history.replaceState({}, "", "/?days=7&account=Personal")
    renderPage()
    const button = await screen.findByRole("button", { name: /Filters/ })
    expect(button.textContent).toContain("2")
  })

  it("offers Try again when the queue fails to load", async () => {
    const original = globalThis.fetch as unknown as (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>
    let failures = 1
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).startsWith("/api/triage") && failures-- > 0) return response({ error: "Database is busy" }, 500)
      return original(input, init)
    }))
    renderPage()
    expect(await screen.findByText(/Database is busy/)).toBeTruthy()
    fireEvent.click(screen.getByRole("button", { name: /Try again/ }))
    expect(await screen.findByRole("option", { name: /Summary 1/ })).toBeTruthy()
  })

  it("labels the undo shortcut with the platform's modifier", async () => {
    renderPage()
    await screen.findByRole("option", { name: /Summary 1/ })
    expect(screen.getByTitle("Undo the most recent Done action").textContent).toMatch(/Undo\s*(Ctrl|⌘)\+Z/)
  })
})

describe("unclassified banner", () => {
  it("stays out of the way when nothing needs a closer look", async () => {
    renderPage()
    await screen.findByRole("option", { name: /Summary 1/ })
    expect(document.querySelector(".attention-banner")).toBeNull()
  })

  it("sits above the mail explorer and retries the whole backlog", async () => {
    unclassified = 16
    renderPage()
    expect(await screen.findByText("16 messages need a closer look")).toBeTruthy()
    const banner = document.querySelector(".attention-banner")!
    expect(banner.compareDocumentPosition(document.querySelector(".triage-bar")!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    fireEvent.click(screen.getByRole("button", { name: "Try classification again" }))
    await waitFor(() => expect(requests.some((request) => request.includes("POST /api/reclassify {}"))).toBe(true))
  })

  it("uses the singular for one message", async () => {
    unclassified = 1
    renderPage()
    expect(await screen.findByText("1 message needs a closer look")).toBeTruthy()
  })

  it("shows live progress and disables the button while a retry is running", async () => {
    unclassified = 16
    jobStatus = { running: true, message: "Re-classifying… 5 of 16" }
    renderPage()
    expect(await screen.findByText("Re-classifying… 5 of 16")).toBeTruthy()
    const button = screen.getByRole("button", { name: "Retrying…" }) as HTMLButtonElement
    expect(button.disabled).toBe(true)
  })

  it("is hidden in the Completed view", async () => {
    unclassified = 16
    window.history.replaceState({}, "", "/?view=completed")
    renderPage()
    await screen.findByRole("option", { name: /Summary 1/ })
    expect(screen.getByRole("tab", { name: /Completed/ }).getAttribute("aria-selected")).toBe("true")
    expect(document.querySelector(".attention-banner")).toBeNull()
  })
})
