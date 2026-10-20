import { FormEvent, KeyboardEvent, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { MessageCircle, RotateCcw, Send, Sparkles, X } from "lucide-react";
import { MAX_MESSAGE_CHARS, useChat, type ChatMessage } from "./useChat";
import { TextBlock } from "./TextBlock";

/**
 * Floating assistant button + panel, shared shape across both apps.
 *
 * Mounted inside Layout, so it only ever appears on authenticated pages - never
 * on login or register, and never for a role without chat access (the server
 * answers 403 and the widget stays closed).
 */
export default function ChatWidget() {
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const { config, messages, busy, loadConfig, send, reset, abort } = useChat();

  const panelRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const logRef = useRef<HTMLDivElement>(null);
  const launcherRef = useRef<HTMLButtonElement>(null);

  const llmOn = Boolean(config?.llm_enabled);

  useEffect(() => {
    void loadConfig();
  }, [loadConfig]);

  // Focus the input on open and hand focus back to the button on close.
  useEffect(() => {
    if (open) {
      inputRef.current?.focus();
    } else {
      abort();
      launcherRef.current?.focus();
    }
  }, [open, abort]);

  // Keep the newest answer in view.
  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [messages, busy]);

  // Escape closes, and Tab is trapped inside the panel while it is open.
  useEffect(() => {
    if (!open) return;
    function onKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape") {
        setOpen(false);
        return;
      }
      if (event.key !== "Tab" || !panelRef.current) return;
      const focusables = panelRef.current.querySelectorAll<HTMLElement>(
        'button, [href], input, [tabindex]:not([tabindex="-1"])',
      );
      if (focusables.length === 0) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [open]);

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    const message = draft.trim();
    // Ignore a double submit: the button is disabled and the draft is cleared.
    if (!message || busy) return;
    setDraft("");
    void send({ message });
  }

  function handleKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      const message = draft.trim();
      if (!message || busy) return;
      setDraft("");
      void send({ message });
    }
  }

  return (
    <>
      {/* Launcher: bottom-right, clear of the header controls, thumb-safe. */}
      <button
        ref={launcherRef}
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-label={open ? "Close assistant" : "Open assistant"}
        aria-expanded={open}
        className="fixed bottom-4 right-4 z-40 flex h-12 w-12 items-center justify-center rounded-full bg-solid text-white shadow-lg transition hover:bg-solid-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-400 focus-visible:ring-offset-2 sm:bottom-6 sm:right-6 sm:h-14 sm:w-14"
      >
        {open ? <X className="h-5 w-5" /> : <MessageCircle className="h-6 w-6" />}
      </button>

      {open && (
        <div
          ref={panelRef}
          role="dialog"
          aria-modal="false"
          aria-label="RAKSHAK Assistant"
          className="fixed inset-x-0 bottom-0 z-50 flex h-[85vh] flex-col overflow-hidden rounded-t-2xl border border-surface-border bg-surface-card text-surface-text shadow-2xl sm:inset-x-auto sm:bottom-24 sm:right-6 sm:h-[32rem] sm:w-[380px] sm:rounded-2xl"
        >
          <header className="flex items-center gap-2 border-b border-surface-border px-4 py-3">
            <Sparkles className="h-4 w-4 text-primary-600" aria-hidden />
            <h2 className="text-sm font-semibold">RAKSHAK Assistant</h2>
            <span
              className="ml-1 inline-flex items-center gap-1 text-xs text-surface-muted"
              title={llmOn ? "AI routing is on" : "Quick answers only"}
            >
              <span
                aria-hidden
                className={`h-2 w-2 rounded-full ${llmOn ? "bg-success" : "bg-warning"}`}
              />
              {llmOn ? "AI on" : "Quick answers only"}
            </span>
            <div className="ml-auto flex items-center gap-1">
              <button
                type="button"
                onClick={reset}
                aria-label="Clear chat"
                className="rounded-lg p-1.5 text-surface-muted transition hover:bg-surface-raised hover:text-surface-text focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-400"
              >
                <RotateCcw className="h-4 w-4" />
              </button>
              <button
                type="button"
                onClick={() => setOpen(false)}
                aria-label="Close assistant"
                className="rounded-lg p-1.5 text-surface-muted transition hover:bg-surface-raised hover:text-surface-text focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-400"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
          </header>

          <div ref={logRef} className="flex-1 space-y-3 overflow-y-auto px-4 py-3">
            {messages.length === 0 && (
              <p className="text-sm text-surface-muted">
                Hello. Ask about reporting a stolen vehicle, your complaints, or privacy.
              </p>
            )}

            {messages.map((message) => (
              <Bubble key={message.id} message={message} onNavigate={navigate} onFollowUp={(text) => !busy && void send({ message: text })} />
            ))}

            {busy && (
              <div className="flex items-center gap-1 text-surface-muted" aria-label="Assistant is typing">
                <span className="h-2 w-2 animate-bounce rounded-full bg-surface-subtle" />
                <span className="h-2 w-2 animate-bounce rounded-full bg-surface-subtle [animation-delay:150ms]" />
                <span className="h-2 w-2 animate-bounce rounded-full bg-surface-subtle [animation-delay:300ms]" />
              </div>
            )}
          </div>

          {/* Quick questions double as the empty state and the follow-up chips. */}
          {config && config.questions.length > 0 && (
            <div className="flex flex-wrap gap-1.5 border-t border-surface-border px-4 py-2">
              {config.questions.slice(0, 6).map((question) => (
                <button
                  key={question.id}
                  type="button"
                  disabled={busy}
                  onClick={() => void send({ quickId: question.id })}
                  className="rounded-full border border-surface-border bg-surface-raised px-2.5 py-1 text-xs text-surface-text transition hover:border-primary-400 disabled:opacity-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-400"
                >
                  {question.label}
                </button>
              ))}
            </div>
          )}

          <form onSubmit={handleSubmit} className="border-t border-surface-border p-3">
            <div className="flex items-center gap-2">
              <input
                ref={inputRef}
                value={draft}
                onChange={(event) => setDraft(event.target.value.slice(0, MAX_MESSAGE_CHARS))}
                onKeyDown={handleKeyDown}
                maxLength={MAX_MESSAGE_CHARS}
                placeholder="Ask a question…"
                aria-label="Your question"
                className="min-w-0 flex-1 rounded-lg border border-surface-border bg-surface-bg px-3 py-2 text-sm text-surface-text placeholder:text-surface-subtle focus:border-primary-400 focus:outline-none"
              />
              <button
                type="submit"
                disabled={busy || !draft.trim()}
                aria-label="Send"
                className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-solid text-white transition hover:bg-solid-hover disabled:opacity-40 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-400"
              >
                <Send className="h-4 w-4" />
              </button>
            </div>
            <p className="mt-1 text-right text-[11px] text-surface-subtle">
              {draft.length}/{MAX_MESSAGE_CHARS}
            </p>
          </form>
        </div>
      )}
    </>
  );
}

function Bubble({
  message,
  onNavigate,
  onFollowUp,
}: {
  message: ChatMessage;
  onNavigate: (path: string) => void;
  onFollowUp: (text: string) => void;
}) {
  const mine = message.role === "user";

  return (
    <div className={`flex ${mine ? "justify-end" : "justify-start"}`}>
      <div
        aria-live={mine ? undefined : "polite"}
        className={`max-w-[85%] rounded-2xl px-3 py-2 text-sm ${
          mine
            ? "bg-solid text-white"
            : message.notice
              ? "border border-warning/40 bg-warning/10 text-surface-text"
              : "border border-surface-border bg-surface-raised text-surface-text"
        }`}
      >
        {!message.pending && (
          <div className="whitespace-pre-wrap break-words">
            <TextBlock text={message.text} />
          </div>
        )}

        {message.kpis && message.kpis.length > 0 && (
          <div className="mt-2 grid grid-cols-2 gap-1.5">
            {message.kpis.map((kpi) => (
              <div key={kpi.label} className="rounded-lg border border-surface-border bg-surface-card px-2 py-1">
                <div className="text-[11px] text-surface-muted">{kpi.label}</div>
                <div className="text-sm font-semibold">{kpi.value}</div>
              </div>
            ))}
          </div>
        )}

        {message.links && message.links.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-1.5">
            {message.links.map((link) => (
              <button
                key={link.path}
                type="button"
                onClick={() => onNavigate(link.path)}
                className="rounded-lg border border-primary-300 bg-surface-card px-2 py-1 text-xs font-medium text-primary-700 transition hover:bg-primary-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-400"
              >
                {link.label}
              </button>
            ))}
          </div>
        )}

        {message.followUps && message.followUps.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-1.5">
            {message.followUps.slice(0, 3).map((text) => (
              <button
                key={text}
                type="button"
                onClick={() => onFollowUp(text)}
                className="rounded-full border border-surface-border bg-surface-card px-2 py-0.5 text-[11px] text-surface-muted transition hover:border-primary-400 hover:text-surface-text focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-400"
              >
                {text}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
