import { describe, expect, it } from "vitest"
import { triageParams } from "./api"

describe("triageParams", () => {
  it("defaults invalid views and ranges while preserving supported filters", () => {
    const result = triageParams("?view=sent&days=180&account=work&category=assessment")
    expect(result.get("view")).toBe("queue")
    expect(result.get("days")).toBe("30")
    expect(result.get("account")).toBe("work")
    expect(result.get("category")).toBe("assessment")
  })

  it("preserves the all mail and completed views", () => {
    expect(triageParams("?view=all&days=7").get("view")).toBe("all")
    expect(triageParams("?view=completed&days=90").get("days")).toBe("90")
  })

  it("ignores empty filters", () => {
    const result = triageParams("?view=queue&account=&category=")
    expect(result.has("account")).toBe(false)
    expect(result.has("category")).toBe(false)
  })
})
