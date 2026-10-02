import { Component, ErrorInfo, ReactNode } from "react";

interface Props {
  children: ReactNode;
  /** Shown so the user knows which app fell over. */
  appName: string;
}

interface State {
  error: Error | null;
}

/**
 * Last line of defence at the app root.
 *
 * React unmounts the whole tree when a render throws, so without this any
 * runtime error (a bad theme token, a hook called outside its provider, a
 * library that fails to initialise) leaves a blank white page and nothing in
 * the UI. Here the message is shown instead, so the cause is readable without
 * opening devtools. In dev the stack is included; in a production build only
 * the message and a reload button are shown, since a stack trace would be
 * noise for a citizen or an officer.
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Keep the stack in the console even in a production build, so a report
    // from a phone is still debuggable.
    console.error(`[${this.props.appName}] unhandled render error`, error, info.componentStack);
  }

  private reset = () => {
    this.setState({ error: null });
  };

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;

    // import.meta.env is untyped here (no vite-env.d.ts), same cast as
    // services/api.ts.
    const isDev = Boolean((import.meta as any).env?.DEV);

    return (
      <div className="flex min-h-screen items-center justify-center bg-surface-bg px-4 py-10">
        <div className="w-full max-w-lg rounded-2xl border border-danger/40 bg-surface-card p-6 shadow-overlay sm:p-8">
          <h1 className="text-xl font-semibold tracking-tight text-surface-text">Something went wrong</h1>
          <p className="mt-2 text-sm text-surface-muted">
            {this.props.appName} could not finish loading this page. The details below tell you what
            failed.
          </p>

          <pre className="mt-4 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-xl border border-surface-border bg-surface-bg p-4 text-left font-mono text-xs leading-relaxed text-danger">
            {error.message || String(error)}
          </pre>

          {isDev && error.stack ? (
            <pre className="mt-3 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-xl border border-surface-border bg-surface-bg p-4 text-left font-mono text-[11px] leading-relaxed text-surface-muted">
              {error.stack}
            </pre>
          ) : null}

          <div className="mt-6 flex flex-wrap gap-3">
            <button
              type="button"
              onClick={() => window.location.reload()}
              className="rounded-lg bg-solid px-4 py-2 text-sm font-medium text-white shadow-sm transition-colors hover:bg-solid-hover"
            >
              Reload page
            </button>
            <button
              type="button"
              onClick={this.reset}
              className="rounded-lg border border-surface-border px-4 py-2 text-sm font-medium text-surface-text transition-colors hover:bg-surface-muted/40"
            >
              Try again
            </button>
          </div>

          <p className="mt-5 text-xs text-surface-muted">
            If this keeps happening: press Ctrl+Shift+R to bypass the cache, then run{" "}
            <code className="font-mono">npm install</code> in the app folder and restart{" "}
            <code className="font-mono">start.bat</code>.
          </p>
        </div>
      </div>
    );
  }
}

export default ErrorBoundary;