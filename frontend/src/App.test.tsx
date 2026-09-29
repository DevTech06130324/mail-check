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
