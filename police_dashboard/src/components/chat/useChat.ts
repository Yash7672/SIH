import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../services/api";

/**
 * Types and the data layer for the assistant panel.
 *
 * Kept separate from the component so the rendering file stays about layout and
 * this file stays about the request lifecycle. All chat state is React state:
 * nothing is persisted, and it is gone on reload or logout.
 */

export interface KpiCard {
  label: string;
  value: string | number;
}

export interface LinkItem {
  label: string;
  path: string;
}

export interface QuickQuestion {
  id: string;
  label: string;
}

export interface ChatConfig {
  enabled: boolean;
  llm_enabled: boolean;
  provider: string;
  questions: QuickQuestion[];
}

export interface ChatAnswer {
  text: string;
  kpis: KpiCard[];
  links: LinkItem[];
  follow_ups: string[];
  questions?: QuickQuestion[];
  kind: string;
  used_llm: boolean;
  tool: string | null;
}

export type ChatRole = "user" | "assistant";

export interface ChatMessage {
  id: string;
  role: ChatRole;
  /** Plain text only - never HTML. See TextBlock. */
  text: string;
  kpis?: KpiCard[];
  links?: LinkItem[];
  followUps?: string[];
  /** Set while an answer is streaming in, to show the typing indicator. */
  pending?: boolean;
  /** Set when this bubble is an error/system notice rather than an answer. */
  notice?: boolean;
}

/** The server caps the message at 500 characters; mirror it in the input. */
export const MAX_MESSAGE_CHARS = 500;

let counter = 0;
export function nextId(): string {
  counter += 1;
  return `m${counter}-${Date.now()}`;
}

/**
 * Turn any thrown value into a sentence a person can act on.
 *
 * The backend already returns friendly 429 text, so that is preferred; anything
 * unrecognised becomes a calm fallback rather than a raw provider error.
 */
export function describeError(error: unknown): { text: string; notice: boolean } {
  const ax = error as { response?: { status?: number; data?: { detail?: unknown }; headers?: Record<string, string> } };
  const status = ax?.response?.status;
  const detail = ax?.response?.data?.detail;

  if (status === 429) {
    const retry = Number(ax?.response?.headers?.["retry-after"]);
    const base = typeof detail === "string" && detail.trim() ? detail.trim() : "Rate limit reached.";
    // The server's own 429 text already names the wait; only add it when it did not.
    if (!/\d/.test(base) && Number.isFinite(retry) && retry > 0) {
      return { text: `${base.replace(/\.$/, "")} Try again in ${Math.ceil(retry)} s.`, notice: true };
    }
    return { text: base.endsWith(".") ? base : `${base}.`, notice: true };
  }

  if (status === 401) {
    return { text: "Session expired. Please sign in again.", notice: true };
  }

  if (status === 503) {
    return { text: "The assistant is switched off right now, but the quick questions still work.", notice: true };
  }

  if (typeof detail === "string" && detail.trim()) {
    return { text: detail, notice: true };
  }

  return { text: "Assistant is offline, quick questions still work.", notice: true };
}

/**
 * Chat client. Holds messages in state and aborts in-flight requests on close.
 */
export function useChat() {
  const [config, setConfig] = useState<ChatConfig | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [busy, setBusy] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  /** Abort any request still in flight (panel closed, component unmounted). */
  const abort = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
  }, []);

  useEffect(() => abort, [abort]);

  const loadConfig = useCallback(async () => {
    try {
      // Paths are relative to `api`'s baseURL, which already ends in /api/v1.
      const { data } = await api.get<ChatConfig>("/chat/config");
      setConfig(data);
    } catch {
      // A failed config still leaves the panel usable: show no chips, no AI.
      setConfig({ enabled: false, llm_enabled: false, provider: "groq", questions: [] });
    }
  }, []);

  const send = useCallback(
    async (payload: { message?: string; quickId?: string }) => {
      abort();
      const controller = new AbortController();
      abortRef.current = controller;

      const promptText =
        payload.message ?? (config?.questions.find((q) => q.id === payload.quickId)?.label ?? "…");

      const userMessage: ChatMessage = { id: nextId(), role: "user", text: promptText };
      const pendingId = nextId();
      setMessages((prev) => [
        ...prev,
        userMessage,
        { id: pendingId, role: "assistant", text: "", pending: true },
      ]);
      setBusy(true);

      try {
        // History is in-memory only and roles are limited to user/assistant.
        const history = messages
          .filter((m) => !m.pending && !m.notice)
          .slice(-4)
          .map((m) => ({ role: m.role, content: m.text.slice(0, MAX_MESSAGE_CHARS) }));

        const { data } = payload.quickId
          ? await api.post<ChatAnswer>("/chat/quick", { id: payload.quickId }, { signal: controller.signal })
          : await api.post<ChatAnswer>(
              "/chat/message",
              { message: (payload.message ?? "").slice(0, MAX_MESSAGE_CHARS), history },
              { signal: controller.signal },
            );

        setMessages((prev) =>
          prev.map((m) =>
            m.id === pendingId
              ? {
                  id: pendingId,
                  role: "assistant",
                  text: data.text,
                  kpis: data.kpis,
                  links: data.links,
                  followUps: data.follow_ups,
                }
              : m,
          ),
        );
      } catch (error) {
        // An abort is a deliberate close, not a failure to report.
        if ((error as { name?: string })?.name === "CanceledError") return;
        if ((error as { code?: string })?.code === "ERR_CANCELED") return;
        const { text, notice } = describeError(error);
        setMessages((prev) =>
          prev.map((m) => (m.id === pendingId ? { id: pendingId, role: "assistant", text, notice } : m)),
        );
      } finally {
        setBusy(false);
        abortRef.current = null;
      }
    },
    [abort, config, messages],
  );

  const reset = useCallback(() => {
    abort();
    setMessages([]);
    setBusy(false);
  }, [abort]);

  return { config, messages, busy, loadConfig, send, reset, abort };
}
