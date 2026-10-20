import { FormEvent, KeyboardEvent, useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { MessageCircle, RotateCcw, Send, ShieldAlert, X } from "lucide-react";
import { MAX_MESSAGE_CHARS, useChat, type ChatMessage } from "./useChat";
import { TextBlock } from "./TextBlock";

/**
 * Floating assistant button + panel for the police console.
 *
 * Mounted inside Layout, so it only ever appears on authenticated pages - never
 * on login or register, and never for a role without chat access (the server
 * answers 403 and the widget stays closed). It reads only: the Live Alerts
 * socket is untouched and no alert state is replayed here.
 *
 * Three behaviours worth knowing:
 *  - The starter questions are a *starter* list, shown only until the first
 *    question is asked. From then on the conversation is driven by the follow-up
 *    chips the server attaches to each answer, so it reads as a chat rather than
 *    a button panel.
 *  - The panel's height is adjustable by dragging its top edge. Only the height
 *    changes: the width is fixed so the transcript never reflows horizontally.
 *  - Every height is clamped against the viewport, so the panel cannot be pushed
 *    off-screen however short the window is or small the drag.
 */

// Height bounds in pixels. MIN_HEIGHT is the drag floor (a header, one exchange
// and the input row); if the window is shorter than that, MIN_USABLE_HEIGHT is
// the floor instead so the panel is merely cramped rather than overflowing.
const MIN_HEIGHT = 320;
const MIN_USABLE_HEIGHT = 240;
const DEFAULT_HEIGHT = 512;
// Space kept between the panel's top edge and the top of the window, so it never
// touches the viewport edge and always leaves the launcher reachable.
const TOP_GAP = 40;

export default function ChatWidget() {
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const [height, setHeight] = useState(DEFAULT_HEIGHT);
  const { config, messages, busy, loadConfig, send, reset, abort } = useChat();

  const panelRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const logRef = useRef<HTMLDivElement>(null);
  const launcherRef = useRef<HTMLButtonElement>(null);

  const llmOn = Boolean(config?.llm_enabled);
  // The starter list is the empty state only: once anything has been asked the
  // answer's own follow-ups take over.
  const showStarters = messages.length === 0 && !busy;

  /** The tallest the panel may be right now, given the viewport. */
  const geometry = useCallback(() => {
    if (typeof window === "undefined") {
      return { max: DEFAULT_HEIGHT, bottom: 96, isSmall: true };
    }
    // The panel normally sits above the launcher (4rem on small screens, 6rem
    // from sm up), but when the window is too short for that the offset has to
    // give way: a cramped panel that fits beats a comfortable one off-screen.
    const isSmall = window.matchMedia("(min-width: 640px)").matches;
    const preferredOffset = isSmall ? 96 : 72;
    const available = window.innerHeight - TOP_GAP;
    // Reserve MIN_USABLE_HEIGHT for the panel and spend whatever is left on the
    // gap above the launcher.
    const bottom = Math.max(0, Math.min(preferredOffset, available - MIN_USABLE_HEIGHT));
    const max = Math.max(MIN_USABLE_HEIGHT, Math.floor(available - bottom));
    return { max, bottom, isSmall };
  }, []);

  const maxHeight = useCallback(() => geometry().max, [geometry]);

  /**
   * The lowest height worth allowing at a given ceiling. Normally MIN_HEIGHT, but
   * on a window too short for even that, the ceiling wins so the panel still fits.
   */
  const floorFor = useCallback((ceiling: number) => Math.min(MIN_HEIGHT, ceiling), []);

  useEffect(() => {
    void loadConfig();
  }, [loadConfig]);

  // Keep the panel within the viewport when the window shrinks or is rotated.
  useEffect(() => {
    if (!open) return;
    function clamp() {
      const ceiling = maxHeight();
      const floor = floorFor(ceiling);
      setHeight((current) => Math.min(ceiling, Math.max(floor, current)));
    }
    clamp();
    window.addEventListener("resize", clamp);
    window.addEventListener("orientationchange", clamp);
    return () => {
      window.removeEventListener("resize", clamp);
      window.removeEventListener("orientationchange", clamp);
    };
  }, [open, maxHeight, floorFor]);

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

  /**
   * Drag the top edge to resize. Pointer events cover mouse, touch and pen in one
   * path, and the pointer is captured so a fast drag that leaves the handle keeps
   * resizing instead of dropping the gesture.
   *
   * Only the height is derived from the pointer: the width is never touched, so
   * the bubbles cannot reflow mid-drag.
   */
  function startResize(event: React.PointerEvent<HTMLDivElement>) {
    event.preventDefault();
    const startY = event.clientY;
    const startHeight = height;
    const ceiling = maxHeight();
    const floor = floorFor(ceiling);
    const handle = event.currentTarget;
    handle.setPointerCapture(event.pointerId);
    document.body.style.userSelect = "none";

    function onMove(move: PointerEvent) {
      // Dragging up (smaller clientY) grows the panel, so the delta is inverted.
      const next = startHeight + (startY - move.clientY);
      setHeight(Math.min(ceiling, Math.max(floor, next)));
    }
    function onUp() {
      handle.releasePointerCapture(event.pointerId);
      handle.removeEventListener("pointermove", onMove);
      handle.removeEventListener("pointerup", onUp);
      handle.removeEventListener("pointercancel", onUp);
      document.body.style.userSelect = "";
    }
    handle.addEventListener("pointermove", onMove);
    handle.addEventListener("pointerup", onUp);
    handle.addEventListener("pointercancel", onUp);
  }

  /** Keyboard equivalent of the drag: arrow keys move the height in steps. */
  function onHandleKey(event: KeyboardEvent<HTMLDivElement>) {
    const ceiling = maxHeight();
    const floor = floorFor(ceiling);
    if (event.key === "ArrowUp") {
      event.preventDefault();
      setHeight((current) => Math.min(ceiling, current + 32));
    } else if (event.key === "ArrowDown") {
      event.preventDefault();
      setHeight((current) => Math.max(floor, current - 32));
    } else if (event.key === "Home") {
      event.preventDefault();
      setHeight(floor);
    } else if (event.key === "End") {
      event.preventDefault();
      setHeight(ceiling);
    }
  }

  function submitDraft() {
    const message = draft.trim();
    if (!message || busy) return;
    setDraft("");
    void send({ message });
  }

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    submitDraft();
  }

  function handleKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submitDraft();
    }
  }

  return (
    <>
      {/* Launcher: bottom-right, clear of the header controls, thumb-safe. It
          follows the same computed offset as the panel, so on a very short window
          the two move down together instead of overlapping. */}
      <button
        ref={launcherRef}
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-label={open ? "Close assistant" : "Open assistant"}
        aria-expanded={open}
        style={{ bottom: `${open ? 16 : Math.max(16, geometry().bottom - 8)}px` }}
        className="fixed right-4 z-40 flex h-12 w-12 items-center justify-center rounded-full bg-solid text-white shadow-lg transition hover:bg-solid-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-400 focus-visible:ring-offset-2 sm:right-6 sm:h-14 sm:w-14"
      >
        {open ? <X className="h-5 w-5" /> : <MessageCircle className="h-6 w-6" />}
      </button>

      {open && (
        <div
          ref={panelRef}
          role="dialog"
          aria-modal="false"
          aria-label="RAKSHAK Assistant"
          // Height comes from state so the user can resize it; the width is fixed
          // and the whole panel is capped against the viewport, so it can never
          // overflow the page. Full-bleed sheet on phones, card from sm up.
          style={{
            height: `${height}px`,
            // The bottom offset is computed in JS so the panel and the launcher
            // agree on the gap, and can shrink together on a very short window.
            bottom: `${geometry().bottom}px`,
          }}
          className="fixed inset-x-0 z-50 flex flex-col overflow-hidden rounded-t-2xl border border-surface-border bg-surface-card text-surface-text shadow-2xl sm:inset-x-auto sm:right-6 sm:w-[380px] sm:rounded-2xl"
        >
          {/* Drag handle: pointer events for touch/mouse, arrow keys for keyboard. */}
          <div
            role="separator"
            aria-orientation="horizontal"
            aria-label="Resize assistant. Use the arrow keys, Home or End."
            aria-valuenow={height}
            aria-valuemin={MIN_HEIGHT}
            aria-valuemax={maxHeight()}
            tabIndex={0}
            onPointerDown={startResize}
            onKeyDown={onHandleKey}
            className="group absolute inset-x-0 top-0 z-10 flex h-4 cursor-ns-resize touch-none items-center justify-center focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-400"
          >
            {/* A short pill, the usual affordance for "drag me". */}
            <span
              aria-hidden
              className="h-1 w-10 rounded-full bg-surface-border transition group-hover:bg-surface-subtle"
            />
          </div>

          {/* pt-4 clears the handle so it never covers the title. */}
          <header className="flex shrink-0 items-center gap-2 border-b border-surface-border px-4 pb-3 pt-4">
            <ShieldAlert className="h-4 w-4 shrink-0 text-primary-600" aria-hidden />
            <h2 className="truncate text-sm font-semibold">RAKSHAK Assistant</h2>
            <span
              className="ml-1 hidden shrink-0 items-center gap-1 text-xs text-surface-muted sm:inline-flex"
              title={llmOn ? "AI routing is on" : "Quick answers only"}
            >
              <span
                aria-hidden
                className={`h-2 w-2 rounded-full ${llmOn ? "bg-success" : "bg-warning"}`}
              />
              {llmOn ? "AI on" : "Quick answers"}
            </span>
            <div className="ml-auto flex shrink-0 items-center gap-1">
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

          {/* min-h-0 lets this flex child actually shrink; without it a long
              conversation would refuse to scroll and grow the panel instead. */}
          <div ref={logRef} className="min-h-0 flex-1 space-y-3 overflow-y-auto px-4 py-3">
            {showStarters && (
              <p className="text-sm text-surface-muted">
                Ask about today's thefts, the hot-list, alerts, complaints or devices.
              </p>
            )}

            {messages.map((message) => (
              <Bubble
                key={message.id}
                message={message}
                onNavigate={navigate}
                onFollowUp={(text) => !busy && void send({ message: text })}
                onAsk={(id) => !busy && void send({ quickId: id })}
                starters={config?.questions ?? []}
              />
            ))}

            {busy && (
              <div className="flex items-center gap-1 text-surface-muted" aria-label="Assistant is typing">
                <span className="h-2 w-2 animate-bounce rounded-full bg-surface-subtle" />
                <span className="h-2 w-2 animate-bounce rounded-full bg-surface-subtle [animation-delay:150ms]" />
                <span className="h-2 w-2 animate-bounce rounded-full bg-surface-subtle [animation-delay:300ms]" />
              </div>
            )}
          </div>

          {/* Starter questions: the empty state only. Once the conversation has
              begun, each answer carries its own follow-ups, so keeping this row
              pinned would just repeat the same six buttons forever. */}
          {showStarters && config && config.questions.length > 0 && (
            <div className="flex max-h-32 shrink-0 flex-wrap gap-1.5 overflow-y-auto border-t border-surface-border px-4 py-2">
              {config.questions.map((question) => (
                <button
                  key={question.id}
                  type="button"
                  disabled={busy}
                  onClick={() => void send({ quickId: question.id })}
                  className="rounded-full border border-surface-border bg-surface-raised px-2.5 py-1 text-left text-xs text-surface-text transition hover:border-primary-400 disabled:opacity-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-400"
                >
                  {question.label}
                </button>
              ))}
            </div>
          )}

          <form onSubmit={handleSubmit} className="shrink-0 border-t border-surface-border p-3">
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
  onAsk,
  starters,
}: {
  message: ChatMessage;
  onNavigate: (path: string) => void;
  onFollowUp: (text: string) => void;
  /** Run a predefined question whose label matched a follow-up exactly. */
  onAsk: (id: string) => void;
  /** The role's predefined questions, so a follow-up can reuse their wording. */
  starters: { id: string; label: string }[];
}) {
  const mine = message.role === "user";

  // A follow-up chip reads best as a chat action: if the server phrased it exactly
  // like a predefined question, run that question (deterministic, no model); the
  // rest are sent as free text and routed normally.
  function followUp(label: string) {
    const match = starters.find(
      (q) => q.label.trim().toLowerCase() === label.trim().toLowerCase(),
    );
    if (match) onAsk(match.id);
    else onFollowUp(label);
  }

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
                onClick={() => followUp(text)}
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
