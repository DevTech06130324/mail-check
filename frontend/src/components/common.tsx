import { createContext, useCallback, useContext, useEffect, useState, type ComponentProps, type ReactNode } from "react"
import { Check, CircleAlert, LoaderCircle, RotateCcw } from "lucide-react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { api, request } from "@/lib/api"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import type { Message } from "@/lib/types"

type Toast = { id: number; text: string; kind: "success" | "error" | "info"; action?: { label: string; run: () => void } }
const ToastContext = createContext<(text: string, kind?: Toast["kind"], action?: Toast["action"]) => void>(() => undefined)

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Toast[]>([])
  const toast = useCallback((text: string, kind: Toast["kind"] = "info", action?: Toast["action"]) => {
    const id = Date.now() + Math.random()
    setItems((current) => [...current, { id, text, kind, action }])
    window.setTimeout(() => setItems((current) => current.filter((item) => item.id !== id)), 6500)
  }, [])
  return <ToastContext.Provider value={toast}>{children}<div className="toast-stack" aria-live="polite">{items.map((item) => <div key={item.id} className={`toast toast-${item.kind}`} role={item.kind === "error" ? "alert" : "status"}>
    {item.kind === "success" ? <Check size={15} /> : item.kind === "error" ? <CircleAlert size={15} /> : null}<span>{item.text}</span>
    {item.action && <Button variant="ghost" size="sm" onClick={item.action.run}>{item.action.label}</Button>}
  </div>)}</div></ToastContext.Provider>
}
export function useToast() { return useContext(ToastContext) }

export function PageHeading({ eyebrow, title, description, action }: { eyebrow?: string; title: string; description?: string; action?: ReactNode }) {
  return <div className="page-heading"><div>{eyebrow && <p className="eyebrow">{eyebrow}</p>}<h1>{title}</h1>{description && <p className="page-description">{description}</p>}</div>{action && <div className="heading-action">{action}</div>}</div>
}

export function Loading({ label = "Loading your workspace…" }: { label?: string }) {
  return <div className="loading-state"><LoaderCircle className="spin" size={19} /><span>{label}</span></div>
}

export function EmptyState({ title, description, action }: { title: string; description: string; action?: ReactNode }) {
  return <div className="empty-state"><span className="empty-mark"><Check size={19} /></span><h2>{title}</h2><p>{description}</p>{action}</div>
}

export function CategoryBadge({ message }: { message: Pick<Message, "category" | "category_label" | "tier" | "deadline_display"> }) {
  const urgent = message.tier === "act" || Boolean(message.deadline_display && /due today|overdue/i.test(message.deadline_display))
  return <Badge variant={urgent ? "urgent" : message.tier === "reply" ? "default" : "muted"}>{message.category_label}</Badge>
}

export function useDoneMutation(onSuccess?: (message: Message) => void) {
  const client = useQueryClient()
  const toast = useToast()
  return useMutation({
    mutationFn: ({ message, done }: { message: Message; done: boolean }) => request<{ ok: boolean; message: string; summary: Record<string, number> }>(`/api/messages/${message.pk}/handled?done=${done ? "true" : "false"}`, { method: "POST" }),
    onSuccess: (_data, variables) => {
      client.invalidateQueries({ queryKey: ["triage"] })
      client.invalidateQueries({ queryKey: ["dashboard"] })
      client.invalidateQueries({ queryKey: ["bootstrap"] })
      onSuccess?.(variables.message)
      const message = variables.message
      const toastText = variables.done ? "Moved to Completed." : "Restored to your queue."
      toast(toastText, "success", variables.done ? { label: "Undo", run: async () => {
        try {
          await api.post("/api/messages/undo-last")
          client.invalidateQueries({ queryKey: ["triage"] })
          client.invalidateQueries({ queryKey: ["dashboard"] })
          client.invalidateQueries({ queryKey: ["bootstrap"] })
          toast(`Restored ${message.company || message.subject}.`, "success")
        } catch (error) { toast(error instanceof Error ? error.message : "Could not undo.", "error") }
      } } : undefined)
    },
    onError: (error) => toast(error instanceof Error ? error.message : "Could not update this message.", "error"),
  })
}

export function SearchInput({ value, onChange, placeholder = "Search" }: { value: string; onChange: (v: string) => void; placeholder?: string }) {
  return <label className="search-field"><span className="sr-only">{placeholder}</span><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7" /><path d="m16 16 4 4" /></svg><input value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder} /></label>
}

export function Switch({ checked, onChange, label }: { checked: boolean; onChange: (v: boolean) => void; label: string }) {
  return <button type="button" role="switch" aria-checked={checked} aria-label={label} onClick={() => onChange(!checked)} className={`switch ${checked ? "switch-on" : ""}`}><span /></button>
}

export function useUndoShortcut(undo: () => void) {
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      const target = event.target
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "z" && !(target instanceof HTMLElement && target.closest("input, textarea, select, [contenteditable=true]"))) {
        event.preventDefault(); undo()
      }
    }
    document.addEventListener("keydown", handler)
    return () => document.removeEventListener("keydown", handler)
  }, [undo])
}

export function QuietButton({ children, busy, ...props }: ComponentProps<typeof Button> & { busy?: boolean }) {
  return <Button {...props} disabled={props.disabled || busy}>{busy && <LoaderCircle className="spin" size={15} />}{children}</Button>
}

export { RotateCcw }
