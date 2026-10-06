import { lazy, Suspense, useCallback, useEffect, useRef, useState } from "react"
import { NavLink, Route, Routes, useLocation, useNavigate } from "react-router-dom"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { Activity, BarChart3, ChevronRight, CircleHelp, Inbox, LoaderCircle, MailCheck, Moon, PanelLeftClose, PanelLeftOpen, Plus, Settings2, Sun, UsersRound } from "lucide-react"
import { api, request } from "@/lib/api"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Loading, PageHeading, ToastProvider, useToast } from "@/components/common"
import type { Bootstrap } from "@/lib/types"
import TriagePage from "@/pages/triage"

const DashboardPage = lazy(() => import("@/pages/dashboard"))
const AccountsPage = lazy(() => import("@/pages/accounts"))
const SettingsPage = lazy(() => import("@/pages/settings"))

const sections = [
  { to: "/", label: "Triage", icon: Inbox },
  { to: "/dashboard", label: "Dashboard", icon: BarChart3 },
  { to: "/accounts", label: "Accounts", icon: UsersRound },
  { to: "/settings", label: "Settings", icon: Settings2 },
]

function ThemeControl() {
  const [theme, setTheme] = useState(() => localStorage.getItem("mailcheck.theme") || "system")
  useEffect(() => {
    const root = document.documentElement
    const apply = () => {
      const dark = theme === "dark" || (theme === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches)
      root.classList.toggle("dark", dark)
      root.style.colorScheme = dark ? "dark" : "light"
    }
    apply()
    const media = window.matchMedia("(prefers-color-scheme: dark)")
    media.addEventListener("change", apply)
    return () => media.removeEventListener("change", apply)
  }, [theme])
  const rotate = () => {
    const next = theme === "system" ? "light" : theme === "light" ? "dark" : "system"
    setTheme(next); localStorage.setItem("mailcheck.theme", next)
  }
  const Icon = theme === "dark" ? Moon : theme === "light" ? Sun : Activity
  return <Button variant="ghost" size="icon" aria-label={`Color theme: ${theme}. Change theme`} title={`Theme: ${theme}`} onClick={rotate}><Icon size={18} /></Button>
}

const formatClock = (seconds: number) => `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`

/** Counts down from the server's `next_in` between polls.
 *
 * The status poll only runs every 30 s while idle, so rendering `next_in`
 * directly froze the clock between polls. This ticks locally off `Date.now()`
 * (accurate even when a background tab throttles timers) and re-baselines on
 * every poll, so the server stays the source of truth. Kept as its own
 * component so only these digits re-render each second.
 */
function Countdown({ nextIn, receivedAt, onDue }: { nextIn: number; receivedAt: number; onDue: () => void }) {
  const [now, setNow] = useState(() => Date.now())
  const announced = useRef(false)
  useEffect(() => {
    announced.current = false
    setNow(Date.now())
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [receivedAt, nextIn])
  const remaining = Math.max(0, nextIn - Math.floor((now - receivedAt) / 1000))
  useEffect(() => {
    if (remaining === 0 && !announced.current) { announced.current = true; onDue() }
  }, [remaining, onDue])
  return <strong className="tabular" aria-live="off">{formatClock(remaining)}</strong>
}

function JobStatus({ bootstrap }: { bootstrap: Bootstrap }) {
  const queryClient = useQueryClient()
  const toast = useToast()
  const [status, setStatusValue] = useState<Awaited<ReturnType<typeof api.status>> | null>(null)
  const [receivedAt, setReceivedAt] = useState(() => Date.now())
  const setStatus = useCallback((next: Awaited<ReturnType<typeof api.status>>) => { setStatusValue(next); setReceivedAt(Date.now()) }, [])
  const seen = useRef<number | null>(null)
  const fastPollUntil = useRef(0)
  const pollNow = useRef<() => void>(() => undefined)
  // When the countdown reaches zero the server is about to start a check:
  // poll quickly for a few seconds instead of waiting out the idle interval.
  const onDue = useCallback(() => {
    if (Date.now() < fastPollUntil.current) return // the fast loop is already polling
    fastPollUntil.current = Date.now() + 10_000
    pollNow.current()
  }, [])
  useEffect(() => {
    let stopped = false
    let timer = 0
    const poll = async () => {
      try {
        const current = await api.status()
        if (stopped) return
        setStatus(current)
        if (seen.current === null) seen.current = current.completion_id
        else if (seen.current !== current.completion_id) {
          seen.current = current.completion_id
          await Promise.all([
            queryClient.invalidateQueries({ queryKey: ["triage"] }),
            queryClient.invalidateQueries({ queryKey: ["dashboard"] }),
            queryClient.invalidateQueries({ queryKey: ["bootstrap"] }),
          ])
          toast(current.message || (current.ok ? "Check complete." : "Check finished with an issue."), current.ok ? "success" : "error")
          try {
            const pref = localStorage.getItem("mailcheck.notify") === "on" && typeof Notification !== "undefined" && Notification.permission === "granted"
            if (pref) {
              const pending = await request<{ items: { pk: number; title: string; who: string; summary: string; url: string }[] }>("/api/notifications")
              const shown: number[] = []
              for (const item of pending.items) {
                try {
                  const alert = new Notification(`${item.title} · ${item.who}`, { body: item.summary, tag: String(item.pk) })
                  alert.onclick = () => { window.open(item.url, "_blank", "noopener"); }
                  shown.push(item.pk)
                } catch { /* Do not acknowledge a notification the browser rejected. */ }
              }
              if (shown.length) await api.post("/api/notifications/ack", shown)
            }
          } catch { /* Notification permissions and browser settings are optional. */ }
        }
      } catch { /* Local console may be starting; retry on the normal cadence. */ }
      if (!stopped) timer = window.setTimeout(poll, status?.running ? 1000 : Date.now() < fastPollUntil.current ? 2000 : 30000)
    }
    const focus = () => { window.clearTimeout(timer); void poll() }
    pollNow.current = focus
    void poll()
    window.addEventListener("focus", focus)
    return () => { stopped = true; window.clearTimeout(timer); window.removeEventListener("focus", focus) }
  }, [queryClient, toast, setStatus, status?.running])

  const runCheck = async () => {
    try {
      await api.post("/api/check", {})
      toast("Check started. Your inbox stays read-only.", "info")
      void api.status().then(setStatus)
    } catch (error) { toast(error instanceof Error ? error.message : "Could not start the check.", "error") }
  }
  const toggle = async () => {
    const enable = !status?.auto
    try {
      const result = await api.post<{ message?: string }>(`/api/autocheck?enabled=${enable}`)
      setStatus(await api.status())
      toast(result.message || (enable ? "Automatic checks are on." : "Automatic checks are off."), "success")
    } catch (error) { toast(error instanceof Error ? error.message : "Could not update the schedule.", "error") }
  }
  const scheduled = Boolean(status?.auto && status.next_in != null)
  const elapsed = status?.running && status.stage_elapsed != null && status.stage_elapsed >= 5 ? status.stage_elapsed : null
  return <div className="top-actions">
    <span className={`connection-status ${status?.running ? "is-running" : ""}`}>
      {status?.running ? <LoaderCircle className="spin" size={15} /> : <span className="status-dot" />}
      {/* Keep changing text in its own element: translators may replace text
          nodes, so removing a bare text sibling can crash React reconciliation.
          Only this message is a live region; the ticking digits are not, or a
          screen reader would announce every second. */}
      <span className="status-text" aria-live="polite">{status?.running ? status.message || "Checking mail…" : scheduled ? "Next check" : "All systems ready"}</span>
      {elapsed != null && <small className="tabular status-elapsed">{elapsed}s</small>}
      {!status?.running && scheduled && status?.next_in != null && <Countdown nextIn={status.next_in} receivedAt={receivedAt} onDue={onDue} />}
    </span>
    <button className={`schedule-button ${status?.auto ? "is-on" : ""}`} type="button" aria-pressed={Boolean(status?.auto)} aria-label={status?.auto ? "Auto-check on" : "Auto-check off"} onClick={toggle} title={status?.auto ? `Checking automatically every ${status.interval_minutes ?? "?"} min. Click to turn off.` : "Turn on automatic checks"}><span className="sched-long">{status?.auto ? "Auto-check on" : "Auto-check off"}</span><span className="sched-short">{status?.auto ? "Auto on" : "Auto off"}</span></button>
    <Button onClick={runCheck} disabled={status?.running || !bootstrap.readiness.model || !bootstrap.readiness.accounts}>{status?.running ? <LoaderCircle className="spin" /> : <Plus />}<span>{status?.running ? "Checking" : "Check now"}</span></Button>
  </div>
}

function Workspace({ bootstrap }: { bootstrap: Bootstrap }) {
  const [collapsed, setCollapsed] = useState(localStorage.getItem("mailcheck.sidebar") === "collapsed")
  const [mobileNav, setMobileNav] = useState(false)
  const location = useLocation()
  const navigate = useNavigate()
  useEffect(() => { setMobileNav(false) }, [location.pathname])
  // The queue size in the tab title is visible from other tabs and the taskbar.
  useEffect(() => { document.title = bootstrap.counts.actionable > 0 ? `(${bootstrap.counts.actionable}) mail-check` : "mail-check" }, [bootstrap.counts.actionable])
  const toggleSidebar = () => { const next = !collapsed; setCollapsed(next); localStorage.setItem("mailcheck.sidebar", next ? "collapsed" : "expanded") }

  return <div className={`app-frame ${collapsed ? "sidebar-collapsed" : ""}`}>
    {mobileNav && <button className="mobile-scrim" aria-label="Close navigation" onClick={() => setMobileNav(false)} />}
    <aside className={`sidebar ${mobileNav ? "mobile-open" : ""}`}>
      <button className="brand-lockup" onClick={() => navigate("/")} aria-label="mail-check home"><span className="brand-mark"><MailCheck size={21} /></span><span className="brand-word">mail<span>check</span></span></button>
      <div className="workspace-label">YOUR WORKSPACE</div>
      <nav className="side-nav" aria-label="Main navigation">{sections.map(({ to, label, icon: Icon }) => <NavLink key={to} to={to} end={to === "/"} className={({ isActive }) => `side-link ${isActive ? "active" : ""}`} title={collapsed ? label : undefined}><Icon size={18} /><span>{label}</span>{label === "Triage" && bootstrap.counts.actionable > 0 && <span className="nav-count">{bootstrap.counts.actionable}</span>}</NavLink>)}</nav>
      <div className="sidebar-bottom">
        <div className="privacy-chip"><span className="privacy-pulse" /><span>Your mail stays local</span></div>
        <div className="sidebar-user"><span className="avatar-mark">m</span><div><strong>mail-check</strong><span>{bootstrap.account_count} {bootstrap.account_count === 1 ? "account" : "accounts"} connected</span></div><CircleHelp size={17} className="help-icon" /></div>
      </div>
    </aside>

    <section className="app-content">
      <header className="topbar">
        <div className="top-left"><Button variant="ghost" size="icon" className="collapse-toggle" onClick={toggleSidebar} aria-label={collapsed ? "Expand navigation" : "Collapse navigation"}>{collapsed ? <PanelLeftOpen /> : <PanelLeftClose />}</Button><Button variant="ghost" size="icon" className="mobile-menu" onClick={() => setMobileNav(true)} aria-label="Open navigation"><PanelLeftOpen /></Button><div className="crumb"><span>Workspace</span><ChevronRight size={14} /><strong>{sections.find((s) => s.to === location.pathname)?.label || "Triage"}</strong></div></div>
        <div className="top-right"><ThemeControl /><JobStatus bootstrap={bootstrap} /></div>
      </header>
      {!bootstrap.readiness.model || !bootstrap.readiness.accounts ? <div className="setup-ribbon"><span><CircleHelp size={17} />Your workspace is almost ready.</span><span>{!bootstrap.readiness.model ? "Connect your Ollama model" : "Connect an email account"}</span><Button variant="outline" size="sm" onClick={() => navigate(!bootstrap.readiness.model ? "/settings" : "/accounts")}>Finish setup <ChevronRight size={15} /></Button></div> : null}
      <main className="page-content"><Suspense fallback={<Loading />}><Routes>
        <Route path="/" element={<TriagePage bootstrap={bootstrap} />} />
        <Route path="/dashboard" element={<DashboardPage bootstrap={bootstrap} />} />
        <Route path="/accounts" element={<AccountsPage />} />
        <Route path="/settings" element={<SettingsPage bootstrap={bootstrap} />} />
        <Route path="*" element={<UnknownRoute />} />
      </Routes></Suspense></main>
      <footer className="app-footer"><span><span className="footer-dot" />Bound to this computer</span><span>Unread stays unread. Always.</span></footer>
    </section>
  </div>
}

function UnknownRoute() {
  const navigate = useNavigate()
  return <div className="page-wrap"><PageHeading title="That page moved" /><Card className="recovery-card"><p>Return to your triage workspace to continue.</p><Button onClick={() => navigate("/")}>Back to triage</Button></Card></div>
}

function Recovery({ message }: { message: string }) {
  const navigate = useNavigate()
  return <main className="recovery-page"><div className="brand-mark"><MailCheck /></div><p className="eyebrow">SAFE RECOVERY</p><h1>Check your local settings</h1><p className="recovery-description">This saved configuration needs an update before mail-check can open. Your mailbox and local mail are unchanged.</p><Card className="recovery-card"><pre>{message}</pre><Button variant="outline" onClick={() => navigate("/settings")}>Try settings</Button></Card><p className="muted">Credentials are never included in this recovery message.</p></main>
}

function ReadyApp() {
  const { data, isPending, error } = useQuery({ queryKey: ["bootstrap"], queryFn: api.bootstrap, retry: false })
  if (isPending) return <Loading />
  if (error) return <Recovery message={error.message} />
  return <Workspace bootstrap={data!} />
}

export default function App() { return <ToastProvider><ReadyApp /></ToastProvider> }
