import { useDeferredValue, useMemo, useState } from "react"
import { useSearchParams } from "react-router-dom"
import { useQuery } from "@tanstack/react-query"
import { CartesianGrid, Bar, BarChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts"
import { CalendarDays, ChevronLeft, ChevronRight, FilterX, Search, SlidersHorizontal } from "lucide-react"
import { api } from "@/lib/api"
import type { Bootstrap, Message } from "@/lib/types"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { EmptyState, Loading, PageHeading, useToast } from "@/components/common"
import { Reader } from "@/pages/triage"
import { useDoneMutation } from "@/components/common"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog"

const colors = ["#0f766e", "#7c9c8f", "#dab574", "#c98c7d", "#9e9ba4", "#8191a3", "#b6a0c0", "#8aa4a1", "#cfaa73", "#aeb2aa"]
const isoToday = () => new Intl.DateTimeFormat("en-CA").format(new Date())
const localCalendarDate = (value: string) => {
  const [year, month, day] = value.slice(0, 10).split("-").map(Number)
  return new Date(year, month - 1, day)
}

export default function DashboardPage({ bootstrap }: { bootstrap: Bootstrap }) {
  const [search, setSearch] = useSearchParams()
  const [searchInput, setSearchInput] = useState(search.get("q") || "")
  const deferredSearch = useDeferredValue(searchInput)
  const [reader, setReader] = useState<Message | null>(null)
  const [advanced, setAdvanced] = useState(false)
  const toast = useToast()
  const done = useDoneMutation(() => setReader(null))
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"
  const params = useMemo(() => {
    const value = new URLSearchParams()
    value.set("days", search.get("days") || "30")
    value.set("interval", search.get("interval") === "week" ? "week" : "day")
    value.set("tz", search.get("tz") || zone)
    for (const key of ["start", "end", "account", "status", "action", "sort", "focus_date", "focus_category"]) if (search.has(key)) value.set(key, search.get(key)!)
    if (deferredSearch.trim()) value.set("q", deferredSearch.trim())
    for (const category of search.getAll("category")) value.append("category", category)
    value.set("page", search.get("page") || "1")
    value.set("page_size", "50")
    return value
  }, [search, zone, deferredSearch])
  const { data, isPending, error, refetch } = useQuery({ queryKey: ["dashboard", params.toString()], queryFn: () => api.dashboard(params) })
  const { data: accounts } = useQuery({ queryKey: ["accounts"], queryFn: api.accounts })
  const update = (changes: Record<string, string | null>) => {
    const next = new URLSearchParams(search)
    for (const [key, value] of Object.entries(changes)) {
      if (key === "category") next.delete("category")
      if (value) next.set(key, value); else next.delete(key)
    }
    if (!("page" in changes)) next.delete("page")
    setSearch(next)
  }
  const toggleCategory = (name: string) => {
    const next = new URLSearchParams(search)
    const selected = next.getAll("category")
    next.delete("category")
    for (const item of selected.includes(name) ? selected.filter((item) => item !== name) : [...selected, name]) next.append("category", item)
    next.delete("page")
    setSearch(next)
  }
  const summary = data?.summary as Record<string, number> | undefined
  const categories = (data?.categories || bootstrap.taxonomy.map((item) => ({ ...item, count: 0 }))) as { name: string; label: string; tier: string; count: number }[]
  const series = data?.daily || []
  const intervalRows = useMemo(() => {
    if (params.get("interval") !== "week" || !series.length) return series
    const weeks = new Map<string, { date: string; total: number; partial: boolean; counts: Record<string, number> }>()
    for (const day of series) {
      const date = new Date(`${day.date}T00:00:00Z`)
      const weekday = date.getUTCDay()
      date.setUTCDate(date.getUTCDate() - ((weekday + 6) % 7))
      const week = date.toISOString().slice(0, 10)
      const item = weeks.get(week) || { date: week, total: 0, partial: false, counts: {} }
      item.total += day.total; item.partial ||= day.partial
      for (const [category, count] of Object.entries(day.counts as Record<string, number>)) item.counts[category] = (item.counts[category] || 0) + count
      weeks.set(week, item)
    }
    return [...weeks.values()]
  }, [series, params])
  const pageInfo = data?.messages as { items: Record<string, any>[]; total: number; page: number; page_size: number; total_pages: number } | undefined
  const filterCount = params.getAll("category").length + ["account", "status", "action", "focus_date", "q"].filter((key) => params.has(key)).length
  const focusCategory = params.get("focus_category")
  const focusDate = params.get("focus_date")
  const clear = () => { setSearch(new URLSearchParams({ tz: zone, days: "30" })); setSearchInput("") }
  const openMessage = (row: Record<string, any>) => {
    setReader({
      ...row, pk: row.pk, message_id: "", subject: row.subject || "(no subject)",
      from_addr: row.from_addr || "", from_name: row.from_name || "", date_utc: row.received_at,
      snippet: row.summary || "", provider_url: null, handled_at: row.handled_at,
      account_label: row.account_label, provider: "imap", category: row.category,
      category_label: row.category_label, tier: row.tier, confidence: 0, company: row.company,
      role: row.role, deadline: null, deadline_display: "",
      date_display: new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" }).format(new Date(row.received_at)),
      date_estimated: row.date_estimated, action_required: row.action_required,
      summary: row.summary || "", source: "llm", open_url: null, open_label: "",
    })
  }
  if (isPending && !data) return <div className="page-wrap"><Loading label="Gathering your local mail history…" /></div>
  if (error && !data) return <div className="page-wrap"><EmptyState title="Dashboard is unavailable" description={error instanceof Error ? error.message : "Try loading this dashboard again."} action={<Button onClick={() => void refetch()}>Try again</Button>} /></div>
  return <div className="page-wrap analytics-page">
    <PageHeading eyebrow="LOCAL MAIL HISTORY" title="Dashboard" description="A little perspective on the applications in motion." action={<Button variant="outline" size="sm" onClick={() => { void refetch(); toast("Activity refreshed.", "success") }}><CalendarDays size={15} />Refresh</Button>} />
    <div className="dashboard-filters"><div className="field-compact"><label htmlFor="range">Date range</label><select id="range" value={params.has("start") ? "custom" : params.get("days") || "30"} onChange={(e) => e.target.value === "custom" ? update({ start: new Date(Date.now() - 29 * 86400000).toISOString().slice(0, 10), end: isoToday() }) : update({ start: null, end: null, days: e.target.value })}><option value="7">Last 7 days</option><option value="30">Last 30 days</option><option value="90">Last 90 days</option><option value="custom">Custom range</option></select></div>
      {params.has("start") && <><div className="field-compact"><label htmlFor="start">From</label><input id="start" type="date" value={params.get("start") || ""} onChange={(e) => update({ start: e.target.value })} /></div><div className="field-compact"><label htmlFor="end">Through</label><input id="end" type="date" value={params.get("end") || ""} onChange={(e) => update({ end: e.target.value })} /></div></>}
      <div className="field-compact"><label htmlFor="interval">Group by</label><select id="interval" value={params.get("interval") || "day"} onChange={(e) => update({ interval: e.target.value })}><option value="day">Day</option><option value="week">Week</option></select></div>
      <div className="field-compact"><label htmlFor="account">Account</label><select id="account" value={params.get("account") || ""} onChange={(e) => update({ account: e.target.value })}><option value="">All accounts</option>{accounts?.accounts.map((account) => <option value={account.label} key={account.id}>{account.label}</option>)}</select></div>
      <div className="field-compact"><label htmlFor="status">Status</label><select id="status" value={params.get("status") || "all"} onChange={(e) => update({ status: e.target.value === "all" ? null : e.target.value })}><option value="all">All mail</option><option value="pending">Open</option><option value="done">Completed</option></select></div>
      <Button variant={advanced ? "secondary" : "outline"} size="sm" onClick={() => setAdvanced((value) => !value)}><SlidersHorizontal size={14} />More</Button>
      {filterCount > 0 && <Button variant="ghost" size="sm" onClick={clear}><FilterX size={14} />Reset</Button>}</div>
    {advanced && <div className="advanced-filters"><label>Action<select value={params.get("action") || "all"} onChange={(e) => update({ action: e.target.value === "all" ? null : e.target.value })}><option value="all">All actions</option><option value="yes">Action required</option><option value="no">No action required</option></select></label><span className="filter-caption">Select categories below to narrow the timeline.</span></div>}
    <div className="stat-grid">{[["Messages collected", summary?.total ?? "—", "stored on this computer"], ["Need your attention", summary?.pending_attention ?? "—", "still in your queue"], ["Completed", summary?.completed ?? "—", "finished by you"], ["Unclassified", summary?.unclassified ?? "—", `${summary?.retryable ?? 0} can be retried`]].map(([label, value, caption], index) => <Card key={String(label)} className="stat-card"><span className="stat-index">0{index + 1}</span><span className="stat-label">{label}</span><strong className="stat-value tabular">{typeof value === "number" ? value.toLocaleString() : value}</strong><span className="stat-caption">{caption}</span></Card>)}</div>
    {focusDate || focusCategory ? <div className="focus-banner"><span>Focused on {focusDate || "all dates"}{focusCategory ? ` · ${categories.find((category) => category.name === focusCategory)?.label || focusCategory}` : ""}</span><Button variant="ghost" size="sm" onClick={() => update({ focus_date: null, focus_category: null })}>Clear focus</Button></div> : null}
    <div className="analytics-main-grid"><Card className="chart-card"><div className="card-title-row"><div><p className="eyebrow">ACTIVITY</p><h2>Applications over time</h2><p className="page-description">Select a bar to focus on that date. Colors follow the filters below.</p></div><Badge variant="muted">{params.get("interval") === "week" ? "By week" : "By day"}</Badge></div>
      <div className="chart-wrap"><ResponsiveContainer width="100%" height="100%"><BarChart data={intervalRows} margin={{ top: 12, right: 8, left: -18, bottom: 0 }} onClick={(e: any) => { if (e?.activePayload?.[0]?.payload?.date) update({ focus_date: e.activePayload[0].payload.date }) }}><CartesianGrid vertical={false} stroke="var(--border)" strokeDasharray="3 4" /><XAxis dataKey="date" tickFormatter={(value: string) => new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" }).format(localCalendarDate(value))} tickLine={false} axisLine={false} minTickGap={32} /><YAxis allowDecimals={false} tickLine={false} axisLine={false} /><Tooltip cursor={{ fill: "var(--muted)", opacity: .48 }} labelFormatter={(value) => new Intl.DateTimeFormat(undefined, { dateStyle: "medium" }).format(localCalendarDate(String(value)))} contentStyle={{ background: "var(--card)", border: "1px solid var(--border)", borderRadius: 12 }} />{categories.map((category, index) => <Bar key={category.name} dataKey={`counts.${category.name}`} name={category.label} stackId="mail" fill={colors[index % colors.length]} radius={index === categories.length - 1 ? [4, 4, 0, 0] : 0} style={{ cursor: "pointer" }} onClick={(item: any) => { if (item?.date) update({ focus_date: item.date, focus_category: category.name }) }} />)}</BarChart></ResponsiveContainer></div>
      <div className="chart-legend">{categories.filter((category) => category.count).map((category, index) => <button key={category.name} onClick={() => toggleCategory(category.name)}><span style={{ backgroundColor: colors[index % colors.length] }} />{category.label}<b>{category.count}</b></button>)}</div>
      <details className="accessible-chart-data"><summary>View chart data as a table</summary><div className="table-scroll"><table><thead><tr><th>Date</th>{categories.map((c) => <th key={c.name}>{c.label}</th>)}<th>Total</th></tr></thead><tbody>{intervalRows.map((row: any) => <tr key={row.date}><th>{row.date}{row.partial ? " *" : ""}</th>{categories.map((c) => <td key={c.name}>{row.counts?.[c.name] || 0}</td>)}<td>{row.total}</td></tr>)}</tbody></table></div></details>
    </Card>
      <Card className="category-card"><div className="card-title-row"><div><p className="eyebrow">AT A GLANCE</p><h2>By category</h2></div></div><div className="category-list">{categories.map((category, index) => <button key={category.name} className={`category-stat ${params.getAll("category").includes(category.name) ? "category-active" : ""}`} onClick={() => toggleCategory(category.name)}><span className="category-swatch" style={{ background: colors[index % colors.length] }} /><span>{category.label}</span><strong className="tabular">{category.count}</strong></button>)}</div><div className="category-footer">Based on {data?.history?.retention_days || 0} days of local history</div></Card></div>
    <section className="explorer"><div className="explorer-heading"><div><p className="eyebrow">MESSAGE EXPLORER</p><h2>Find a message</h2><p className="page-description">Search and read mail already collected on this computer.</p></div><label className="search-field explorer-search"><Search size={16} /><input value={searchInput} onChange={(e) => { setSearchInput(e.target.value); update({ q: e.target.value || null }) }} placeholder="Search subject, sender, company…" aria-label="Search stored mail" /></label></div>
      <div className="category-pills" aria-label="Filter categories"><button className={!params.getAll("category").length ? "pill-active" : ""} onClick={() => update({ category: null })}>All categories</button>{categories.filter((c) => c.count).map((c) => <button key={c.name} className={params.getAll("category").includes(c.name) ? "pill-active" : ""} onClick={() => update({ category: params.getAll("category").includes(c.name) ? null : c.name })}>{c.label}</button>)}</div>
      {pageInfo?.items.length ? <><div className="table-scroll explorer-table"><table><thead><tr><th>Message</th><th>Category</th><th>Account</th><th>Received</th></tr></thead><tbody>{pageInfo.items.map((row) => <tr key={row.pk} tabIndex={0} onClick={() => openMessage(row)} onKeyDown={(event) => { if (event.key === "Enter") openMessage(row) }}><td><strong>{row.company || row.from_name || row.from_addr}</strong><span>{row.subject}</span></td><td><Badge variant="muted">{row.category_label}</Badge></td><td>{row.account_label}</td><td>{new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" }).format(new Date(row.received_at))}</td></tr>)}</tbody></table></div><div className="pagination"><span>{pageInfo.total ? `${(pageInfo.page - 1) * pageInfo.page_size + 1}–${Math.min(pageInfo.page * pageInfo.page_size, pageInfo.total)} of ${pageInfo.total}` : "0 messages"}</span><div><Button variant="outline" size="sm" aria-label="Previous page" disabled={pageInfo.page <= 1} onClick={() => update({ page: String(pageInfo.page - 1) })}><ChevronLeft size={15} />Previous</Button><Button variant="outline" size="sm" aria-label="Next page" disabled={pageInfo.page >= pageInfo.total_pages} onClick={() => update({ page: String(pageInfo.page + 1) })}>Next<ChevronRight size={15} /></Button></div></div></> : <EmptyState title="No messages match" description="Adjust your search, filters, or date range to find more stored mail." action={<Button variant="outline" onClick={clear}>Clear filters</Button>} />}
      {data?.daily?.some((row: any) => row.partial) && <p className="partial-note">* Some dates have partial collection history. Previously unread mail and deleted mail are not available.</p>}
    </section>
    <Dialog open={Boolean(reader)} onOpenChange={(open) => !open && setReader(null)}><DialogContent className="dashboard-reader-dialog"><DialogHeader><DialogTitle>Stored message</DialogTitle><DialogDescription>Read-only mail collected by mail-check.</DialogDescription></DialogHeader>{reader && <Reader message={reader} mobile={true} onBack={() => setReader(null)} onDone={() => done.mutate({ message: reader, done: !reader.handled_at })} busy={done.isPending} canRetry={bootstrap.readiness.model} />}</DialogContent></Dialog>
  </div>
}
