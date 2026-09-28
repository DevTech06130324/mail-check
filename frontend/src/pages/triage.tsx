import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type Ref } from "react"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { ArrowLeft, ArrowUpRight, CalendarClock, Check, ChevronDown, Clock3, Filter, LoaderCircle, RefreshCw, RotateCcw, Sparkles } from "lucide-react"
import { api, triageParams } from "@/lib/api"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { CategoryBadge, EmptyState, Loading, PageHeading, useDoneMutation, useToast, useUndoShortcut } from "@/components/common"
import type { Bootstrap, Message, TriageData } from "@/lib/types"

function PersonLine({ message }: { message: Message }) {
  const sender = message.from_name || message.from_addr || "Unknown sender"
  return <div className="sender-line"><span className="sender-avatar">{sender.slice(0, 1).toUpperCase()}</span><span className="sender-copy"><strong>{sender}</strong><span>{message.from_addr}</span></span><span className="sender-date">{message.date_display}{message.date_estimated && <Badge variant="muted">estimated</Badge>}</span></div>
}

export function Reader({ message, mobile, onBack, onDone, busy, canRetry = false, readerRef }: { message: Message | null; mobile: boolean; onBack: () => void; onDone: () => void; busy: boolean; canRetry?: boolean; readerRef?: Ref<HTMLElement> }) {
  const queryClient = useQueryClient()
  const toast = useToast()
  const [retrying, setRetrying] = useState(false)
  const { data, isPending, error } = useQuery({ queryKey: ["reader", message?.pk], queryFn: () => api.reader(message!.pk), enabled: Boolean(message), staleTime: 60_000 })
  if (!message) return <section className={`reader-empty ${mobile ? "mobile-visible" : ""}`}><span className="reader-empty-icon"><Sparkles size={18} /></span><h2>Choose a message</h2><p>Select a message from your queue and its full story will appear here.</p><p className="shortcut-hint"><kbd>↑</kbd><kbd>↓</kbd> to move <span>·</span> <kbd>E</kbd> to mark done</p></section>
  const retryClassification = async () => {
    if (retrying) return
    setRetrying(true)
    try {
      await api.post("/api/reclassify", { pks: [message.pk] })
      toast("Classification retry started for this message.", "success")
      queryClient.invalidateQueries({ queryKey: ["status"] })
    } catch (error) { toast(error instanceof Error ? error.message : "Could not retry classification.", "error") }
    finally { setRetrying(false) }
  }
  return <article ref={readerRef} tabIndex={-1} className={`reader-pane ${mobile ? "mobile-visible" : ""}`}>
    <div className="reader-toolbar"><Button className="reader-back" variant="ghost" size="sm" onClick={onBack}><ArrowLeft size={16} /> All messages</Button><span className="reader-account">{message.account_label}</span><div className="reader-actions">{message.category === "unclassified" && canRetry && <Button variant="outline" size="sm" onClick={retryClassification} disabled={retrying}>{retrying ? <LoaderCircle className="spin" size={14} /> : <RefreshCw size={14} />}Try classification again</Button>}{message.open_url && <Button variant="outline" size="sm" asChild><a href={message.open_url} target="_blank" rel="noopener">{message.open_label}<ArrowUpRight size={14} /></a></Button>}<Button size="sm" onClick={onDone} disabled={busy}><Check size={15} />{message.handled_at ? "Restore" : "Done"}</Button></div></div>
    <div className="reader-content" key={message.pk}>
      <div className="reader-category"><CategoryBadge message={message} />{message.deadline_display && <span className="deadline-line"><CalendarClock size={15} />{message.deadline_display}</span>}</div>
      <h2 className="reader-subject">{message.subject || "(no subject)"}</h2>
      <PersonLine message={message} />
      {message.summary && <div className="insight-card"><span className="insight-icon"><Sparkles size={15} /></span><p>{message.summary}</p></div>}
      <dl className="message-meta">{message.company && <div><dt>Company</dt><dd>{message.company}</dd></div>}{message.role && <div><dt>Role</dt><dd>{message.role}</dd></div>}<div><dt>Inbox</dt><dd>{message.account_label}</dd></div>{message.confidence > 0 && message.confidence < .5 && <div><dt>Confidence</dt><dd>Low ({Math.round(message.confidence * 100)}%)</dd></div>}{message.source === "prefilter" && <div><dt>Classified by</dt><dd>Local rule</dd></div>}</dl>
      <div className="reader-divider" />
      {isPending ? <Loading label="Opening message…" /> : error ? <div className="inline-error">{error instanceof Error ? error.message : "Could not open this message."}</div> : <div className="email-body" dangerouslySetInnerHTML={{ __html: data?.body_html || "<p>No message body was stored.</p>" }} />}
    </div>
  </article>
}

export default function TriagePage({ bootstrap }: { bootstrap: Bootstrap }) {
  const queryClient = useQueryClient()
  const toast = useToast()
  const [params, setParams] = useState(() => new URLSearchParams(window.location.search))
  const [selected, setSelected] = useState<number | null>(null)
  const [readerOpen, setReaderOpen] = useState(false)
  const [filtersOpen, setFiltersOpen] = useState(false)
  const listRef = useRef<HTMLDivElement>(null)
  const readerRef = useRef<HTMLElement>(null)
  useEffect(() => {
    const sync = () => setParams(new URLSearchParams(window.location.search))
    window.addEventListener("popstate", sync)
    return () => window.removeEventListener("popstate", sync)
  }, [])
  const normalized = useMemo(() => triageParams(params.toString()), [params])
  const query = useQuery({ queryKey: ["triage", normalized.toString()], queryFn: () => api.triage(normalized), placeholderData: (previous) => previous })
  const data = query.data as TriageData | undefined
  const messages = data?.groups.flatMap((group) => group.items) || []
  const selectedMessage = messages.find((message) => message.pk === selected) || null
  const view = normalized.get("view") as TriageData["view"]
  const title = view === "queue" ? data?.summary.actionable ? `${data.summary.actionable} to handle` : "All clear" : view === "all" ? "All mail" : "Completed"
  const setFilter = (name: string, value: string) => {
    const next = new URLSearchParams(normalized)
    if (!value) next.delete(name); else next.set(name, value)
    const search = next.toString()
    history.pushState(null, "", `${window.location.pathname}${search ? `?${search}` : ""}`)
    setParams(next)
  }
  const focusSelection = useCallback((pk: number | null, readerVisible = readerOpen) => {
    requestAnimationFrame(() => {
      const compact = typeof window.matchMedia === "function" && window.matchMedia("(max-width: 1023px)").matches
      if (compact && readerVisible) {
        readerRef.current?.focus({ preventScroll: true })
        return
      }
      const target = pk === null ? listRef.current : listRef.current?.querySelector<HTMLElement>(`[data-pk="${pk}"]`)
      target?.focus({ preventScroll: true })
      target?.scrollIntoView({ block: "nearest" })
    })
  }, [readerOpen])
  const selectMessage = useCallback((pk: number) => {
    setSelected(pk)
    setReaderOpen(true)
    focusSelection(pk, true)
  }, [focusSelection])
  useEffect(() => {
    if (messages.length && !messages.some((message) => message.pk === selected)) setSelected(messages[0].pk)
    if (!messages.length) setSelected(null)
  }, [messages, selected])
  const done = useDoneMutation()
  const undo = useCallback(async () => {
    try {
      const response = await api.post<{ message: string }>("/api/messages/undo-last")
      await Promise.all([queryClient.invalidateQueries({ queryKey: ["triage"] }), queryClient.invalidateQueries({ queryKey: ["bootstrap"] })])
      toast(response.message || "Restored to the queue.", "success")
    } catch (error) { toast(error instanceof Error ? error.message : "Nothing to undo.", "error") }
  }, [queryClient, toast])
  useUndoShortcut(() => { void undo() })
  const setDone = (message: Message) => {
    if (done.isPending) return
    const target = !message.handled_at
    const index = messages.findIndex((item) => item.pk === message.pk)
    const wasSelected = message.pk === selected
    const next = messages[index + 1] || messages[index - 1]
    if (wasSelected) setSelected(next?.pk ?? null)
    done.mutate({ message, done: target }, {
      onSuccess: () => { if (wasSelected) focusSelection(next?.pk ?? null) },
      onError: () => { if (wasSelected) { setSelected(message.pk); focusSelection(message.pk) } },
    })
  }

  const actKey = useCallback((event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.target instanceof HTMLElement && event.target.closest("input, select, textarea, [contenteditable=true]")) return
    if (!messages.length || (event.ctrlKey || event.metaKey || event.altKey)) return
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault()
      const index = messages.findIndex((message) => message.pk === selected)
      const next = index < 0 ? (event.key === "ArrowDown" ? 0 : messages.length - 1) : Math.max(0, Math.min(messages.length - 1, index + (event.key === "ArrowDown" ? 1 : -1)))
      selectMessage(messages[next].pk)
    } else if (event.key.toLowerCase() === "e" && selectedMessage && !done.isPending) setDone(selectedMessage)
  }, [messages, selected, selectedMessage, done.isPending, selectMessage])

  const retryAll = async () => {
    try { await api.post("/api/reclassify", {}); toast("Classification retry started.", "success"); queryClient.invalidateQueries({ queryKey: ["status"] }) }
    catch (error) { toast(error instanceof Error ? error.message : "Could not start retry.", "error") }
  }

  if (query.isPending) return <div className="page-wrap"><Loading /></div>
  if (query.error && !data) return <div className="page-wrap"><div className="inline-error">Could not load triage. {query.error.message}</div></div>
  const waitingSetup = !bootstrap.readiness.model || !bootstrap.readiness.accounts
  const noMessages = !messages.length
  const unclassifiedCount = data?.counts.unclassified ?? 0
  return <div className="page-wrap triage-page" onKeyDown={actKey}>
    <PageHeading eyebrow={view === "queue" ? "YOUR NEXT MOVE" : view === "all" ? "THE FULL PICTURE" : "A JOB WELL DONE"} title={title || "Your triage"} description={data?.last_run || bootstrap.last_run || undefined} action={<Button variant="ghost" size="sm" onClick={undo} title="Undo the most recent Done action"><RotateCcw size={15} />Undo <kbd>⌘Z</kbd></Button>} />
    <div className="triage-bar"><div className="view-tabs" role="tablist" aria-label="Mail views">{(["queue", "all", "completed"] as const).map((key) => <button type="button" role="tab" aria-selected={view === key} className={view === key ? "selected" : ""} key={key} onClick={() => setFilter("view", key)}>{key === "queue" ? "Queue" : key === "all" ? "All mail" : "Completed"}{key === "queue" && data?.summary.actionable ? <span className="tab-count">{data.summary.actionable}</span> : null}</button>)}</div>
      <div className="triage-tools"><Button variant="outline" size="sm" className={filtersOpen ? "tool-active" : ""} onClick={() => setFiltersOpen((current) => !current)}><Filter size={14} />Filters<ChevronDown size={13} /></Button><span className="list-count">{data?.total.toLocaleString()} messages</span></div></div>
    {filtersOpen && <div className="filter-panel"><label>Mailbox<select value={normalized.get("account") || ""} onChange={(e) => setFilter("account", e.target.value)}><option value="">All mailboxes</option>{data?.accounts.map((account) => <option key={account.label} value={account.label}>{account.label}</option>)}</select></label><label>Category<select value={normalized.get("category") || ""} onChange={(e) => setFilter("category", e.target.value)}><option value="">All categories</option>{bootstrap.taxonomy.filter((category) => data?.counts[category.name]).map((category) => <option key={category.name} value={category.name}>{category.label} · {data?.counts[category.name]}</option>)}</select></label><label>Time range<select value={normalized.get("days") || "30"} onChange={(e) => setFilter("days", e.target.value)}>{[2, 3, 7, 14, 30, 90].map((days) => <option key={days} value={days}>Last {days} days</option>)}</select></label>{(normalized.has("account") || normalized.has("category") || normalized.get("days") !== "30") && <Button variant="ghost" size="sm" onClick={() => { const next = new URLSearchParams("view=" + view); history.pushState(null, "", `/?${next}`); setParams(next) }}>Clear filters</Button>}</div>}
    {view === "queue" && unclassifiedCount > 0 && bootstrap.readiness.model && <div className="attention-banner"><span><Sparkles size={15} />{unclassifiedCount} message{unclassifiedCount === 1 ? "" : "s"} need a closer look</span><Button variant="outline" size="sm" onClick={retryAll}><RefreshCw size={14} />Try classification again</Button></div>}
    {waitingSetup && noMessages ? <EmptyState title="A thoughtful inbox starts here" description={!bootstrap.readiness.model ? "Connect your Ollama model in Settings. It runs where you choose, and your mailbox remains read-only." : "Connect a mail account to start building your private application inbox."} action={<Button onClick={() => window.location.assign(!bootstrap.readiness.model ? "/settings" : "/accounts")}>Continue setup</Button>} /> : noMessages ? <EmptyState title={view === "completed" ? "No completed messages yet" : view === "queue" ? "Your queue is clear" : "No mail in this range"} description={view === "queue" ? "Nothing needs your attention right now. Your mail is still right where you left it." : "Try another date range or clear a filter to see more messages."} action={view === "queue" && data?.summary.informational ? <Button variant="outline" onClick={() => setFilter("view", "all")}>Take a look at all mail</Button> : undefined} /> : <div className={`triage-split ${readerOpen ? "reader-is-open" : ""}`}>
      <div className="message-list" ref={listRef} role="listbox" aria-label="Triage messages" tabIndex={-1}>{data?.groups.map((group) => <section key={group.tier} className="tier-section"><div className={`tier-label tier-${group.tier}`}><span className="tier-dot" /><span>{group.label}</span><span className="tier-rule" /><span className="tier-total">{group.items.length}</span></div>{group.items.map((message) => <button role="option" aria-selected={selected === message.pk} tabIndex={selected === message.pk ? 0 : -1} key={message.pk} data-pk={message.pk} className={`mail-row ${selected === message.pk ? "mail-selected" : ""} ${message.handled_at ? "mail-done" : ""}`} onClick={() => selectMessage(message.pk)} onDoubleClick={() => setDone(message)}>
        <span className="mail-row-top"><CategoryBadge message={message} />{message.deadline_display && <span className={`row-deadline ${/overdue|today/i.test(message.deadline_display) ? "deadline-urgent" : ""}`}><Clock3 size={12} />{message.deadline_display}</span>}<span className="row-date">{message.date_display}</span></span>
        <span className="mail-row-title">{message.company && message.role ? `${message.company} · ${message.role}` : message.company || message.role || message.from_name || message.from_addr}</span><span className="mail-account">{message.account_label}</span><span className="mail-row-summary">{message.summary || message.snippet || message.subject}</span>
      </button>)}</section>)}</div>
      <Reader message={selectedMessage} mobile={readerOpen} readerRef={readerRef} onBack={() => { setReaderOpen(false); focusSelection(selectedMessage?.pk ?? null, false) }} onDone={() => selectedMessage && setDone(selectedMessage)} busy={done.isPending} canRetry={bootstrap.readiness.model} />
    </div>}
    <div className="keyboard-note"><span><kbd>↑</kbd><kbd>↓</kbd> navigate</span><span><kbd>E</kbd> done</span><span><kbd>⌘</kbd><kbd>Z</kbd> undo</span></div>
  </div>
}
