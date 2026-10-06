import { useEffect, useState, type FormEvent } from "react"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { ExternalLink, KeyRound, LoaderCircle, Mail, Plus, ShieldCheck, Trash2, UserRound, Wifi } from "lucide-react"
import { api } from "@/lib/api"
import type { Account } from "@/lib/types"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Card } from "@/components/ui/card"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { EmptyState, Loading, PageHeading, Switch, useToast } from "@/components/common"

function AccountRow({ account, onRefresh }: { account: Account; onRefresh: () => void }) {
  const [busy, setBusy] = useState("")
  const toast = useToast()
  const change = async (action: string, url: string) => {
    setBusy(action)
    try {
      const result = await api.post<{ message?: string }>(url)
      toast(result.message || (action === "test" ? "Connection looks good." : "Account updated."), "success")
      onRefresh()
    } catch (error) { toast(error instanceof Error ? error.message : "Account action failed.", "error") }
    finally { setBusy("") }
  }
  const label = encodeURIComponent(account.label)
  return <div className="account-row"><div className="account-icon">{account.provider === "outlook" ? <Mail size={19} /> : <UserRound size={19} />}</div><div className="account-info"><div className="account-name-line"><h3>{account.label}</h3><Badge variant={account.provider === "outlook" ? "default" : "outline"}>{account.provider === "outlook" ? "Microsoft" : "IMAP"}</Badge><Badge variant={account.enabled ? "muted" : "outline"}>{account.enabled ? "Active" : "Paused"}</Badge></div><p>{account.email}{account.provider === "imap" ? ` · ${account.imap_host} · ${account.folder}` : " · Personal Microsoft account"}</p></div><div className="account-actions"><span className="account-toggle-label">Include in checks</span><Switch checked={account.enabled} label={`Include ${account.label} in checks`} onChange={(enabled) => void change("toggle", `/api/accounts/${label}/toggle?enabled=${enabled}`)} /><Button variant="outline" size="sm" disabled={Boolean(busy)} onClick={() => void change("test", `/api/accounts/${label}/test`)}>{busy === "test" ? <LoaderCircle className="spin" /> : <Wifi size={14} />}Test</Button><Button variant="ghost" size="icon" className="account-remove" title={`Remove ${account.label}`} disabled={Boolean(busy)} onClick={() => { if (window.confirm(`Remove ${account.label}? Its local message history will stay until normal retention cleanup.`)) void change("remove", `/api/accounts/${label}/delete`) }}><Trash2 size={15} /></Button></div></div>
}

function AddImapDialog({ open, onOpenChange, refresh }: { open: boolean; onOpenChange: (open: boolean) => void; refresh: () => void }) {
  const toast = useToast()
  const [email, setEmail] = useState("")
  const [label, setLabel] = useState("")
  const [password, setPassword] = useState("")
  const [host, setHost] = useState("")
  const [port, setPort] = useState(993)
  const [folder, setFolder] = useState("INBOX")
  const [note, setNote] = useState("")
  const [pending, setPending] = useState(false)
  const applyPreset = async (value: string) => {
    if (!value.includes("@")) return
    try {
      const result = await fetch(`/api/preset?email=${encodeURIComponent(value)}`).then((response) => response.json())
      const preset = result.preset as { host: string; port: number; note: string } | null
      if (!preset) { setNote("Add your provider’s IMAP hostname below."); return }
      setHost(preset.host); setPort(preset.port); setNote(preset.note)
      if (!label) setLabel(value.split("@")[0].replace(/[^a-z0-9_-]/gi, "-").slice(0, 28))
    } catch { setNote("Enter the IMAP server details provided by your email provider.") }
  }
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setPending(true)
    try {
      const result = await api.post<{ message: string }>("/api/accounts", { email, label, password, host, port, use_ssl: true, folder })
      toast(result.message || "Mailbox connected.", "success")
      setEmail(""); setLabel(""); setPassword(""); setHost(""); setFolder("INBOX"); setNote("")
      onOpenChange(false); refresh()
    } catch (error) { toast(error instanceof Error ? error.message : "Could not connect this mailbox.", "error") }
    finally { setPending(false) }
  }
  const close = (next: boolean) => { if (!next) setPassword(""); onOpenChange(next) }
  return <Dialog open={open} onOpenChange={close}><DialogContent><DialogHeader><DialogTitle>Connect a mailbox</DialogTitle><DialogDescription>mail-check connects with read-only access. Your app password is saved only in your OS keyring.</DialogDescription></DialogHeader><form onSubmit={submit} className="form-stack"><label>Email address<input type="email" autoComplete="email" required value={email} onChange={(event) => { setEmail(event.target.value); void applyPreset(event.target.value) }} placeholder="you@gmail.com" /></label><label>Account label<input required maxLength={80} value={label} onChange={(event) => setLabel(event.target.value)} placeholder="Personal" /></label><label>App password<input type="password" autoComplete="new-password" required value={password} onChange={(event) => setPassword(event.target.value)} placeholder="Provider app password" /><small>Never paste your everyday mailbox password. Credentials are not sent to Ollama.</small></label><details className="advanced-field"><summary>Server details</summary><div className="settings-grid"><label>IMAP host<input required value={host} onChange={(event) => setHost(event.target.value)} placeholder="imap.gmail.com" /></label><label>Port<input type="number" min={1} max={65535} value={port} onChange={(event) => setPort(Number(event.target.value))} /></label><label>Folder<input value={folder} onChange={(event) => setFolder(event.target.value)} /></label></div></details>{note && <p className="form-hint">{note}</p>}<div className="dialog-footer"><Button variant="outline" type="button" onClick={() => close(false)}>Cancel</Button><Button disabled={pending}>{pending && <LoaderCircle className="spin" />}Test &amp; connect</Button></div></form></DialogContent></Dialog>
}

function OutlookDialog({ open, onOpenChange, refresh }: { open: boolean; onOpenChange: (open: boolean) => void; refresh: () => void }) {
  const [label, setLabel] = useState("")
  const [flow, setFlow] = useState<{ user_code: string; verification_uri: string } | null>(null)
  const [message, setMessage] = useState("")
  const [pending, setPending] = useState(false)
  const toast = useToast()
  useEffect(() => {
    if (!open || !flow) return
    let cancelled = false
    let timer = 0
    const poll = async () => {
      try {
        const response = await fetch("/api/outlook/status")
        const status = await response.json()
        if (cancelled) return
        if (status.state === "connected") { setMessage(status.message); setFlow(null); refresh(); toast(status.message || "Outlook connected.", "success"); onOpenChange(false); return }
        if (status.state === "failed") { setMessage(status.message); setFlow(null); return }
      } catch { /* Retry while the local console is available. */ }
      if (!cancelled) timer = window.setTimeout(poll, 2000)
    }
    void poll()
    return () => { cancelled = true; clearTimeout(timer) }
  }, [flow, open, onOpenChange, refresh, toast])
  const cancel = async () => { try { await api.post("/api/outlook/cancel") } catch { /* State may already be idle. */ } setFlow(null); onOpenChange(false) }
  const start = async (event: FormEvent) => {
    event.preventDefault(); setPending(true); setMessage("")
    try {
      const response = await api.post<{ user_code: string; verification_uri: string }>("/api/outlook/start", { label })
      setFlow(response); window.open(response.verification_uri, "_blank", "noopener,noreferrer")
    } catch (error) { setMessage(error instanceof Error ? error.message : "Could not start Microsoft sign-in.") }
    finally { setPending(false) }
  }
  return <Dialog open={open} onOpenChange={(next) => { if (!next && flow) void cancel(); else onOpenChange(next) }}><DialogContent><DialogHeader><DialogTitle>Connect Outlook.com</DialogTitle><DialogDescription>Sign in with Microsoft’s device code. mail-check requests read-only Mail.Read access.</DialogDescription></DialogHeader>{flow ? <div className="outlook-code"><p>Open the Microsoft sign-in page and enter this code:</p><strong>{flow.user_code}</strong><a href={flow.verification_uri} target="_blank" rel="noreferrer">Open Microsoft sign-in <ExternalLink size={14} /></a><p className="form-hint">This dialog checks for completion automatically.</p><div className="dialog-footer"><Button variant="outline" onClick={() => void cancel()}>Cancel sign-in</Button></div></div> : <form className="form-stack" onSubmit={start}><label>Account label<input autoFocus value={label} onChange={(event) => setLabel(event.target.value)} required placeholder="Personal Outlook" /></label>{message && <p role="alert" className="inline-error">{message}</p>}<div className="dialog-footer"><Button variant="outline" type="button" onClick={() => onOpenChange(false)}>Cancel</Button><Button disabled={pending}>{pending && <LoaderCircle className="spin" />}Continue with Microsoft</Button></div></form>}</DialogContent></Dialog>
}

export default function AccountsPage() {
  const [imapOpen, setImapOpen] = useState(false)
  const [outlookOpen, setOutlookOpen] = useState(false)
  const queryClient = useQueryClient()
  const { data, isPending, error } = useQuery({ queryKey: ["accounts"], queryFn: api.accounts })
  const refresh = () => { void queryClient.invalidateQueries({ queryKey: ["accounts"] }); void queryClient.invalidateQueries({ queryKey: ["bootstrap"] }) }
  if (isPending) return <div className="page-wrap"><Loading /></div>
  return <div className="page-wrap accounts-page"><PageHeading eyebrow="YOUR MAILBOXES" title="Accounts" description="Connect the inboxes behind your next opportunity." action={<Button onClick={() => setImapOpen(true)}><Plus size={16} />Connect mailbox</Button>} />
    <div className="security-note"><ShieldCheck size={18} /><span><strong>Read-only by design.</strong> Mail-check never marks messages as read, sends replies, or changes your provider inbox.</span></div>
    {error && <p className="inline-error" role="alert">{error.message}</p>}
    {data?.accounts.length ? <><div className="section-title-line"><div><p className="eyebrow">CONNECTED INBOXES</p><h2>Your accounts</h2></div><span className="account-total">{data.accounts.length} total</span></div><Card className="account-card">{data.accounts.map((account) => <AccountRow key={account.id} account={account} onRefresh={refresh} />)}</Card></> : <EmptyState title="One inbox at a time" description="Connect a mailbox to begin building your private application queue." action={<Button onClick={() => setImapOpen(true)}><Plus size={16} />Connect your first mailbox</Button>} />}
    <div className="provider-heading"><div><p className="eyebrow">CONNECTION OPTIONS</p><h2>Choose your provider</h2></div><span>Credentials stay on your computer.</span></div>
    <div className="provider-grid"><button className="provider-card" onClick={() => setImapOpen(true)}><span className="provider-logo"><KeyRound /></span><span><strong>Gmail, Yahoo, iCloud &amp; more</strong><small>Connect with a secure app password over IMAP.</small></span><Plus size={17} className="provider-add" /></button><button className="provider-card" onClick={() => data?.outlook_ready ? setOutlookOpen(true) : (window.location.href = "/settings#outlook")}><span className="provider-logo provider-microsoft">M</span><span><strong>Personal Outlook</strong><small>{data?.outlook_ready ? "Sign in with Microsoft. No password is stored." : "Add your Microsoft client ID in Settings first."}</small></span><Plus size={17} className="provider-add" /></button></div>
    <Card className="privacy-card"><ShieldCheck size={18} /><div><strong>Your mail stays yours.</strong><p>Messages and email bodies live in your local database. Only sender, subject, and the configured body excerpt go to your chosen Ollama model. Microsoft 365 work and school accounts are not supported.</p></div></Card>
    <AddImapDialog open={imapOpen} onOpenChange={setImapOpen} refresh={refresh} /><OutlookDialog open={outlookOpen} onOpenChange={setOutlookOpen} refresh={refresh} />
  </div>
}
