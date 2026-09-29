import { Component, type ErrorInfo, type ReactNode } from "react"
import { Button } from "@/components/ui/button"

type Props = { children: ReactNode }
type State = { error: Error | null; componentStack: string }

export class AppErrorBoundary extends Component<Props, State> {
  state: State = { error: null, componentStack: "" }

  static getDerivedStateFromError(error: unknown): Partial<State> {
    return { error: error instanceof Error ? error : new Error(String(error)) }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("mail-check failed while rendering", error, info.componentStack)
    this.setState({ componentStack: info.componentStack || "" })
  }

  render() {
    if (this.state.error) {
      return <main className="recovery-page" role="alert">
        <div className="brand-mark" aria-hidden="true">m</div>
        <p className="eyebrow">SAFE RECOVERY</p>
        <h1>Mail-check couldn’t open this page</h1>
        <p className="recovery-description">An error interrupted the page. Reload to try again. If it happens again, share the technical details below.</p>
        <Button onClick={() => window.location.reload()}>Reload page</Button>
        <details className="recovery-card" style={{ marginTop: "1.5rem", textAlign: "left" }}>
          <summary>Technical details</summary>
          <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{this.state.error.stack || this.state.error.message}{this.state.componentStack}</pre>
        </details>
      </main>
    }
    return this.props.children
  }
}
