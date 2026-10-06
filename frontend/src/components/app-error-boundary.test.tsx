import { afterEach, describe, expect, it, vi } from "vitest"
import { cleanup, render, screen } from "@testing-library/react"
import { AppErrorBoundary } from "./app-error-boundary"

afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe("AppErrorBoundary", () => {
  it("shows a recovery action instead of leaving the app blank after a render crash", () => {
    function BrokenPage(): never { throw new Error("render failed") }
    vi.spyOn(console, "error").mockImplementation(() => undefined)

    render(<AppErrorBoundary><BrokenPage /></AppErrorBoundary>)

    expect(screen.getByRole("alert").textContent).toContain("couldn’t open")
    expect(screen.getByRole("button", { name: /reload/i })).toBeTruthy()
    const details = screen.getByText("Technical details").closest("details")!
    expect(details.textContent).toContain("Error: render failed")
    expect(details.textContent).toContain("BrokenPage")
  })
})
