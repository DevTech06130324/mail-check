import { StrictMode } from "react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { MemoryRouter } from "react-router-dom"
import App from "./App"
import { AppErrorBoundary } from "@/components/app-error-boundary"
import { api } from "@/lib/api"

type Status = Awaited<ReturnType<typeof api.status>>
const idle: Status = { running: false, auto: false, next_in: null, completion_id: 1, message: "", detail: "", ok: true, stage_elapsed: null }
const scheduled: Status = { ...idle, auto: true, next_in: 120 }
const running: Status = { ...scheduled, running: true, message: "Reading mail…" }
const clients: QueryClient[] = []

beforeEach(() => {
  vi.spyOn(console, "error").mockImplementation(() => undefined)
  vi.stubGlobal("localStorage", { getItem: vi.fn(() => null), setItem: vi.fn() })
  vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })))
  vi.spyOn(api, "bootstrap").mockResolvedValue({
    readiness: { model: true, accounts: true, privacy_ack: true, outlook: false },
    counts: { actionable: 0 }, taxonomy: [], presets: [], last_run: "Never checked", account_count: 1,
  })
  vi.spyOn(api, "triage").mockResolvedValue({
    view: "queue", groups: [], total: 0, summary: {}, counts: {}, accounts: [],
    days: 30, selected_account: null, selected_category: null,
  })
})

afterEach(() => {
  cleanup()
  clients.splice(0).forEach((client) => client.clear())
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

// Browser translators replace React-owned text nodes with their own elements.
// Reproduce that DOM transformation without requiring an installed extension.
function replaceTextNodes(root: Element) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  const nodes: Node[] = []
  while (walker.nextNode()) nodes.push(walker.currentNode)
  for (const node of nodes) {
    const wrapper = document.createElement("font")
    wrapper.textContent = node.textContent
    node.parentNode!.replaceChild(wrapper, node)
  }
}

describe("header status after external text replacement", () => {
  it.each([
    ["idle to scheduled", idle, scheduled, "Next check", "2:00"],
    ["scheduled to running", scheduled, running, "Reading mail…", null],
    ["running to completed", running, { ...scheduled, completion_id: 2 }, "Next check", "2:00"],
    ["scheduled to idle", scheduled, idle, "All systems ready", null],
    ["progress message", running, { ...running, message: "Classifying mail…" }, "Classifying mail…", null],
    ["countdown tick", scheduled, { ...scheduled, next_in: 119 }, "Next check", "1:59"],
  ] as const)("survives %s and displays the latest status", async (_name, initial, next, label, countdown) => {
    let current: Status = initial
    vi.spyOn(api, "status").mockImplementation(async () => current)
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
    clients.push(client)
    const { container } = render(<StrictMode><AppErrorBoundary><QueryClientProvider client={client}>
      <MemoryRouter><App /></MemoryRouter>
    </QueryClientProvider></AppErrorBoundary></StrictMode>)
    await screen.findByText("Your queue is clear")
    const status = container.querySelector(".connection-status")!
    await waitFor(() => expect(status.textContent).toContain(initial.running ? initial.message : initial.auto ? "Next check" : "All systems ready"))
    replaceTextNodes(status)

    current = next
    await act(async () => { fireEvent.focus(window) })

    expect(screen.queryByRole("alert")?.querySelector("pre")?.textContent || "").toBe("")
    expect(container.querySelector(".connection-status")?.textContent).toContain(label)
    expect(container.querySelector(".connection-status strong")?.textContent ?? null).toBe(countdown)
    expect(screen.getByRole("navigation", { name: "Main navigation" })).toBeTruthy()
    expect(console.error).not.toHaveBeenCalled()
  })
})

describe("live countdown", () => {
  // Fake only the interval and the clock: react-query, waitFor and the status
  // poll all rely on real setTimeout.
  beforeEach(() => { vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "Date"] }) })
  afterEach(() => { vi.useRealTimers() })

  const mount = () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
    clients.push(client)
    return render(<QueryClientProvider client={client}><MemoryRouter><App /></MemoryRouter></QueryClientProvider>)
  }
  const digits = (container: HTMLElement) => container.querySelector(".connection-status strong")?.textContent ?? null

  it("counts down every second without asking the server again", async () => {
    const status = vi.spyOn(api, "status").mockResolvedValue({ ...scheduled, next_in: 120 })
    const { container } = mount()
    await waitFor(() => expect(digits(container)).toBe("2:00"))
    const calls = status.mock.calls.length

    await act(async () => { vi.advanceTimersByTime(5000) })

    expect(digits(container)).toBe("1:55")
    expect(status.mock.calls.length).toBe(calls)
  })

  it("re-baselines on the server's value after a poll", async () => {
    let current: Status = { ...scheduled, next_in: 120 }
    vi.spyOn(api, "status").mockImplementation(async () => current)
    const { container } = mount()
    await waitFor(() => expect(digits(container)).toBe("2:00"))
    await act(async () => { vi.advanceTimersByTime(5000) })

    current = { ...scheduled, next_in: 200 }
    await act(async () => { fireEvent.focus(window) })

    expect(digits(container)).toBe("3:20")
  })

  it("polls right away at zero and switches to the running message", async () => {
    let current: Status = { ...scheduled, next_in: 3 }
    const status = vi.spyOn(api, "status").mockImplementation(async () => current)
    const { container } = mount()
    await waitFor(() => expect(digits(container)).toBe("0:03"))
    const calls = status.mock.calls.length

    current = { ...running, message: "Reading mail…" }
    await act(async () => { vi.advanceTimersByTime(3000) })

    await waitFor(() => expect(container.querySelector(".connection-status")?.textContent).toContain("Reading mail…"))
    expect(status.mock.calls.length).toBeGreaterThan(calls)
  })

  it("keeps the ticking digits out of the live region", async () => {
    vi.spyOn(api, "status").mockResolvedValue({ ...scheduled, next_in: 60 })
    const { container } = mount()
    await waitFor(() => expect(digits(container)).toBe("1:00"))
    expect(container.querySelector(".connection-status")?.getAttribute("aria-live")).toBeNull()
    expect(container.querySelector(".connection-status strong")?.closest("[aria-live=polite]")).toBeNull()
  })

  it("shows the schedule as a pressed toggle", async () => {
    vi.spyOn(api, "status").mockResolvedValue({ ...scheduled, interval_minutes: 10 })
    mount()
    const toggle = await screen.findByRole("button", { name: "Auto-check on" })
    expect(toggle.getAttribute("aria-pressed")).toBe("true")
  })
})
