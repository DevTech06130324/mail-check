import { useState, type ReactNode } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Check, LoaderCircle, Plus, Save, ShieldCheck, Sparkles, Wifi } from "lucide-react"
import { api } from "@/lib/api"
import type { Bootstrap, Settings } from "@/lib/types"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { EmptyState, Loading, PageHeading, Switch, useToast } from "@/components/common"

function Field({ label, hint, children, className = "" }: { label: string; hint?: string; children: ReactNode; className?: string }) {
  return <label className={`setting-field ${className}`}><span>{label}</span>{children}{hint && <small>{hint}</small>}</label>
}

export default function SettingsPage({ bootstrap }: { bootstrap: Bootstrap }) {
  const queryClient = useQueryClient()
  const toast = useToast()
  const { data, isPending, error } = useQuery({ queryKey: ["settings"], queryFn: api.settings })
  const [draft, setDraft] = useState<Settings | null>(null)
  const [notify, setNotify] = useState(() => localStorage.getItem("mailcheck.notify") === "on")
  const [category, setCategory] = useState("job_alert")
  const [sender, setSender] = useState("")
  const [domain, setDomain] = useState("")
  const [subject, setSubject] = useState("")
  const [testing, setTesting] = useState(false)
  const settings = draft || data?.settings || null
  const dirty = Boolean(draft && data && JSON.stringify(draft) !== JSON.stringify(data.settings))
  const rules = data?.rules || []
  const update = (group: keyof Settings, key: string, value: unknown) => {
    if (!settings) return
    const section = { ...(settings[group] as object), [key]: value }
    setDraft({ ...settings, [group]: section } as Settings)
  }
  const save = useMutation({ mutationFn: (payload: Settings) => api.post<{ message: string }>("/api/settings", {
    base_url: payload.llm.base_url, model: payload.llm.model, batch_size: payload.llm.batch_size,
    max_body_chars: payload.llm.max_body_chars, timeout_seconds: payload.llm.timeout_seconds,
    classification_deadline_seconds: payload.llm.classification_deadline_seconds,
    concurrency: payload.llm.concurrency, num_ctx: payload.llm.num_ctx,
    think: payload.llm.think, keep_alive: payload.llm.keep_alive,
    lookback_days: payload.check.lookback_days, retain_days: payload.check.retain_days,
    interval_minutes: payload.watch.interval_minutes, auto_check: payload.watch.auto_check,
    outlook_client_id: payload.outlook.client_id, privacy_ack: payload.privacy_ack,
  }), onSuccess: (result) => { toast(result.message || "Settings saved.", "success"); void queryClient.invalidateQueries({ queryKey: ["settings"] }); void queryClient.invalidateQueries({ queryKey: ["bootstrap"] }); setDraft(null) }, onError: (reason) => toast(reason instanceof Error ? reason.message : "Could not save settings.", "error") })
  const testModel = async () => {
    setTesting(true)
    try { const result = await api.post<{ message: string }>("/api/settings/test"); toast(result.message || "Connection looks good.", "success") }
    catch (reason) { toast(reason instanceof Error ? reason.message : "Model connection failed.", "error") }
    finally { setTesting(false) }
  }
  const addRule = useMutation({ mutationFn: () => api.post<{ message: string }>("/api/rules", { category, sender: sender.trim() || null, sender_domain: domain.trim() || null, subject_contains: subject.trim() || null }), onSuccess: () => { toast("Local rule added.", "success"); setSender(""); setDomain(""); setSubject(""); void queryClient.invalidateQueries({ queryKey: ["settings"] }) }, onError: (reason) => toast(reason instanceof Error ? reason.message : "Could not add this rule.", "error") })
  const removeRule = useMutation({ mutationFn: (index: number) => api.post(`/api/rules/${index}/delete`), onSuccess: () => { toast("Rule removed.", "success"); void queryClient.invalidateQueries({ queryKey: ["settings"] }) }, onError: (reason) => toast(reason instanceof Error ? reason.message : "Could not remove this rule.", "error") })
  const privacy = async (accepted: boolean) => {
    if (!settings) return
    const updated = { ...settings, privacy_ack: accepted }
    setDraft(updated)
    try {
      await api.post("/api/settings", { privacy_ack: accepted })
      await queryClient.invalidateQueries({ queryKey: ["bootstrap"] })
      setDraft(null)
      toast(accepted ? "Privacy acknowledgement saved." : "Privacy acknowledgement cleared.", "success")
    } catch (reason) { toast(reason instanceof Error ? reason.message : "Could not save acknowledgement.", "error") }
  }
  const notification = async (enabled: boolean) => {
    if (enabled && "Notification" in window && Notification.permission === "default") {
      const permission = await Notification.requestPermission()
      if (permission !== "granted") { toast("Browser notification permission was not granted.", "error"); return }
    }
    setNotify(enabled); localStorage.setItem("mailcheck.notify", enabled ? "on" : "off")
    toast(enabled ? "Urgent mail notifications are on in this browser." : "Urgent mail notifications are off.", "success")
  }
  const testNotification = () => {
    try { const note = new Notification("mail-check", { body: "Urgent mail notifications are ready." }); note.onclick = () => window.focus(); toast("Test notification sent.", "success") }
    catch { toast("Could not show a notification. Check this browser’s site permissions.", "error") }
  }
  if (isPending) return <div className="page-wrap"><Loading label="Opening your local settings…" /></div>
  if (error || !data || !settings) return <div className="page-wrap"><EmptyState title="Settings could not be loaded" description={error instanceof Error ? error.message : "Try again shortly."} action={<Button onClick={() => void queryClient.invalidateQueries({ queryKey: ["settings"] })}>Try again</Button>} /></div>
  return <div className="page-wrap settings-page">
    <PageHeading eyebrow="YOUR LOCAL SETUP" title="Settings" description="Make mail-check feel at home on your machine." action={<Button onClick={() => save.mutate(settings)} disabled={!dirty || save.isPending}><Save size={15} />{save.isPending ? "Saving…" : dirty ? "Save changes" : "All saved"}</Button>} />
    {!bootstrap.readiness.model || !bootstrap.readiness.accounts ? <Card className="setup-checklist"><span className="setup-icon"><Sparkles size={17} /></span><div><p className="eyebrow">A SMALL FIRST-RUN CHECKLIST</p><h2>Three little steps to begin</h2><p>Connect a model, add a mailbox, and run your first check when you’re ready.</p><div className="checklist-steps"><a href="#model"><span className={bootstrap.readiness.model ? "step-done" : ""}>{bootstrap.readiness.model ? <Check size={13} /> : "1"}</span>Connect Ollama</a><a href="/accounts"><span className={bootstrap.readiness.accounts ? "step-done" : ""}>{bootstrap.readiness.accounts ? <Check size={13} /> : "2"}</span>Connect a mailbox</a><a href="/"><span>3</span>Review your triage</a></div></div></Card> : null}
    <div className="settings-layout"><nav className="settings-nav" aria-label="Settings sections">{[["model", "Model"], ["checking", "Checking"], ["notifications", "Notifications"], ["outlook", "Outlook"], ["rules", "Local rules"], ["privacy", "Privacy"]].map(([id, label]) => <a href={`#${id}`} key={id}>{label}</a>)}</nav>
      <div className="settings-sections">
        <section id="model" className="settings-section"><div className="settings-section-title"><span className="section-symbol"><Sparkles size={17} /></span><div><h2>Model</h2><p>Choose where classification runs.</p></div>{bootstrap.readiness.model && <Badge variant="default"><span className="status-dot" />Connected</Badge>}</div><Card className="settings-card"><Field label="Ollama server URL" hint="Enter the server root. Do not include /v1 or /api/chat."><input autoComplete="url" value={settings.llm.base_url} onChange={(e) => update("llm", "base_url", e.target.value)} placeholder="http://localhost:11434" /></Field><Field label="Model name" hint="Use the model name already installed on your Ollama server."><input value={settings.llm.model} onChange={(e) => update("llm", "model", e.target.value)} placeholder="qwen3.5:35b-a3b" /></Field><details className="advanced-field"><summary>Advanced inference settings</summary><div className="settings-grid"><Field label="Context size" hint="Larger contexts may need more server memory."><input type="number" min="1024" max="262144" value={settings.llm.num_ctx} onChange={(e) => update("llm", "num_ctx", Number(e.target.value))} /></Field><Field label="Request timeout · seconds"><input type="number" min="5" max="60" value={settings.llm.timeout_seconds} onChange={(e) => update("llm", "timeout_seconds", Number(e.target.value))} /></Field><Field label="Classification deadline · seconds"><input type="number" min="5" max="300" value={settings.llm.classification_deadline_seconds} onChange={(e) => update("llm", "classification_deadline_seconds", Number(e.target.value))} /></Field><Field label="Keep model loaded"><input value={settings.llm.keep_alive} onChange={(e) => update("llm", "keep_alive", e.target.value)} placeholder="5m" /></Field><Field label="Emails per request"><input type="number" min="1" max="32" value={settings.llm.batch_size} onChange={(e) => update("llm", "batch_size", Number(e.target.value))} /></Field><Field label="Body characters sent to model"><input type="number" min="200" max="8000" value={settings.llm.max_body_chars} onChange={(e) => update("llm", "max_body_chars", Number(e.target.value))} /></Field></div><div className="switch-row"><div><strong>Enable model thinking</strong><small>May improve results with supported models and increase run time.</small></div><Switch checked={settings.llm.think} label="Enable model thinking" onChange={(checked) => update("llm", "think", checked)} /></div></details><Button variant="outline" size="sm" disabled={testing || !settings.llm.base_url || !settings.llm.model} onClick={() => void testModel()}>{testing ? <LoaderCircle className="spin" /> : <Wifi size={15} />}{testing ? "Testing connection…" : "Test connection"}</Button></Card></section>
        <section id="checking" className="settings-section"><div className="settings-section-title"><span className="section-symbol"><LoaderCircle size={17} /></span><div><h2>Checking</h2><p>Set a light rhythm for mailbox checks.</p></div></div><Card className="settings-card"><div className="switch-row"><div><strong>Check automatically</strong><small>Runs only while the mail-check console is open. Off by default.</small></div><Switch checked={settings.watch.auto_check} label="Check automatically" onChange={(checked) => update("watch", "auto_check", checked)} /></div><div className="settings-grid"><Field label="Check every · minutes"><input type="number" min="1" max="1440" value={settings.watch.interval_minutes} onChange={(e) => update("watch", "interval_minutes", Number(e.target.value))} /></Field><Field label="Look back · days" hint="Unread mailbox window fetched each run."><input type="number" min="1" max="3650" value={settings.check.lookback_days} onChange={(e) => update("check", "lookback_days", Number(e.target.value))} /></Field><Field label="Keep local mail · days" hint="Older mail is removed from this computer on a future check."><input type="number" min="1" max="3650" value={settings.check.retain_days} onChange={(e) => update("check", "retain_days", Number(e.target.value))} /></Field></div><div className="form-hint">Automatic checks stop when you close the console. Use <code>mail-check watch</code> for unattended runs.</div></Card></section>
        <section id="notifications" className="settings-section"><div className="settings-section-title"><span className="section-symbol"><Check size={17} /></span><div><h2>Notifications</h2><p>Choose what this browser brings to your attention.</p></div></div><Card className="settings-card"><div className="switch-row"><div><strong>Urgent mail alerts</strong><small>Notify once for interviews, timed assessments, and offers.</small></div><Switch checked={notify} label="Urgent mail browser notifications" onChange={(checked) => void notification(checked)} /></div>{notify && <Button variant="outline" size="sm" onClick={testNotification}>Test notification</Button>}<p className="form-hint">Notification permission is saved by your browser. This preference stays in this browser.</p></Card></section>
        <section id="outlook" className="settings-section"><div className="settings-section-title"><span className="section-symbol">M</span><div><h2>Outlook</h2><p>For personal Outlook.com, Hotmail, or Live.</p></div>{bootstrap.readiness.outlook && <Badge variant="muted">Ready</Badge>}</div><Card className="settings-card"><Field label="Microsoft application (client) ID" hint="Public identifier only. Register a public client with delegated Mail.Read and device-code flow enabled."><input value={settings.outlook.client_id} onChange={(e) => update("outlook", "client_id", e.target.value)} placeholder="00000000-0000-0000-0000-000000000000" /></Field><p className="form-hint">Tokens remain in your OS keyring. Microsoft 365 work and school accounts are not supported.</p></Card></section>
        <section id="rules" className="settings-section"><div className="settings-section-title"><span className="section-symbol"><ShieldCheck size={17} /></span><div><h2>Local rules</h2><p>Label known senders before email reaches the model.</p></div></div><Card className="settings-card">{rules.length ? <div className="rules-list">{rules.map((rule, index) => <div className="rule-row" key={`${index}-${rule.category}`}><Badge variant="muted">{data.categories.find((item) => item.name === rule.category)?.label || rule.category}</Badge><span>{[rule.sender && `from ${rule.sender}`, rule.sender_domain && `domain ${rule.sender_domain}`, rule.subject_contains && `subject “${rule.subject_contains}”`].filter(Boolean).join(" · ")}</span><Button variant="ghost" size="sm" onClick={() => removeRule.mutate(index)} disabled={removeRule.isPending}>Remove</Button></div>)}</div> : <p className="form-hint">No local rules yet. Known job-board digests are filtered automatically.</p>}<div className="settings-grid rule-fields"><Field label="Label as"><select value={category} onChange={(e) => setCategory(e.target.value)}>{data.categories.map((item) => <option key={item.name} value={item.name}>{item.label}</option>)}</select></Field><Field label="Sender address"><input value={sender} onChange={(e) => setSender(e.target.value)} placeholder="alerts@example.com" /></Field><Field label="Sender domain"><input value={domain} onChange={(e) => setDomain(e.target.value)} placeholder="example.com" /></Field><Field label="Subject contains"><input value={subject} onChange={(e) => setSubject(e.target.value)} placeholder="Application received" /></Field></div><Button size="sm" variant="outline" onClick={() => addRule.mutate()} disabled={addRule.isPending}><Plus size={14} />Add local rule</Button></Card></section>
        <section id="privacy" className="settings-section"><div className="settings-section-title"><span className="section-symbol"><ShieldCheck size={17} /></span><div><h2>Privacy &amp; local data</h2><p>You stay in control of what crosses your device.</p></div></div><Card className="settings-card privacy-settings"><p>Sender, subject, and up to <strong>{settings.llm.max_body_chars.toLocaleString()}</strong> characters of each email body go to your configured Ollama server for classification. Mailbox credentials stay in your OS keyring, and provider mail remains read-only.</p><label className="acknowledge-row"><input type="checkbox" checked={settings.privacy_ack} onChange={(e) => void privacy(e.target.checked)} /><span><strong>I understand and accept this</strong><small>Recorded locally as your explicit privacy acknowledgement.</small></span></label><dl><div><dt>Config file</dt><dd>{data.config_path}</dd></div><div><dt>Local database</dt><dd>{data.db_path}</dd></div></dl></Card></section>
        <div className="settings-bottom-action"><span>{dirty ? "You have unsaved changes." : "Your preferences are saved on this computer."}</span><Button onClick={() => save.mutate(settings)} disabled={!dirty || save.isPending}><Save size={15} />{save.isPending ? "Saving…" : "Save changes"}</Button></div>
      </div>
    </div>
  </div>
}
