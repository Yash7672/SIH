import { useCallback, useEffect, useRef, useState } from "react";
import { AlertEvent, WS_BASE, getValidAccessToken } from "../services/api";

/**
 * Connection state shown to the operator.
 *
 * The old hook exposed a bare `connected` boolean, so a socket stuck in a
 * reconnect loop looked identical to one that was cleanly closed - there was no
 * way to tell "waiting to come back" from "gone for good".
 */
export type SocketStatus = "connecting" | "live" | "reconnecting" | "offline";

export interface PoliceSocketState {
  status: SocketStatus;
  /** True only while the socket is actually open and usable. */
  connected: boolean;
  /** ms timestamp of the last alert received, or null before the first one. */
  lastEventAt: number | null;
  /** Human-readable reason for the current state, for tooltips and the footer. */
  detail: string | null;
  /** Force an immediate reconnect (used by the manual retry control). */
  reconnectNow: () => void;
}

/** Backoff schedule in ms. The last value repeats. */
const BACKOFF_MS = [1000, 2000, 5000, 10000, 15000];
/**
 * The browser's own keepalive is far too slow to notice a dead hotspot, so an
 * application ping surfaces a drop within seconds instead of minutes.
 */
const HEARTBEAT_MS = 25000;

function backoffFor(attempt: number): number {
  return BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)];
}

function describeClose(event: CloseEvent): string {
  switch (event.code) {
    case 4401:
      return "session expired (4401)";
    case 4403:
      return "this account cannot read police alerts (4403)";
    case 1008:
      return "connection rejected by policy (1008)";
    case 1006:
      return "network unreachable (1006)";
    default:
      return event.reason ? `${event.reason} (${event.code})` : `closed (${event.code})`;
  }
}

export function usePoliceSocket(onAlert: (a: AlertEvent) => void): PoliceSocketState {
  const [status, setStatus] = useState<SocketStatus>("connecting");
  const [detail, setDetail] = useState<string | null>(null);
  const [lastEventAt, setLastEventAt] = useState<number | null>(null);

  const wsRef = useRef<WebSocket | null>(null);
  const cbRef = useRef(onAlert);
  cbRef.current = onAlert;

  const retryRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const heartbeatRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const attemptRef = useRef(0);
  const connectingRef = useRef(false);
  /**
   * StrictMode guard.
   *
   * React 18 mounts, unmounts and remounts every component in development. The
   * previous cleanup called ws.close(), and the socket's own onclose handler then
   * scheduled `setTimeout(connect, 3000)` - so the remount opened a *second*
   * socket while the first was still closing, and that orphaned retry kept
   * firing after the component was gone. Every timer and handler below is gated
   * on this flag so teardown really is final.
   */
  const mountedRef = useRef(true);
  /** Held in a ref so `connect` can stay referentially stable. */
  const connectRef = useRef<() => void>(() => {});

  const clearTimers = useCallback(() => {
    if (retryRef.current) {
      clearTimeout(retryRef.current);
      retryRef.current = null;
    }
    if (heartbeatRef.current) {
      clearInterval(heartbeatRef.current);
      heartbeatRef.current = null;
    }
  }, []);

  const scheduleReconnect = useCallback(
    (reason: string) => {
      if (!mountedRef.current) return;
      clearTimers();

      const delay = backoffFor(attemptRef.current);
      attemptRef.current += 1;
      setStatus(typeof navigator !== "undefined" && navigator.onLine === false ? "offline" : "reconnecting");
      setDetail(reason);

      retryRef.current = setTimeout(() => {
        retryRef.current = null;
        connectRef.current();
      }, delay);
    },
    [clearTimers]
  );

  const connect = useCallback(async () => {
    if (!mountedRef.current || connectingRef.current) return;
    connectingRef.current = true;

    // Always take a token that is valid *now*. Access tokens live 30 minutes and
    // the old hook read localStorage once per connect, so after a refresh with an
    // expired token the server closed the socket with 4401 and every reconnect
    // replayed the same dead token forever.
    const token = await getValidAccessToken();
    connectingRef.current = false;

    if (!mountedRef.current) return;
    if (!token) {
      setStatus("offline");
      setDetail("no valid session - sign in again");
      return;
    }

    setStatus(attemptRef.current === 0 ? "connecting" : "reconnecting");
    setDetail(null);

    let ws: WebSocket;
    try {
      // WS_BASE is derived from window.location at runtime, so it survives an IP
      // change, and it is always ws:// (never http://) on the backend's port.
      ws = new WebSocket(`${WS_BASE}/api/v1/ws/police?token=${encodeURIComponent(token)}`);
    } catch {
      scheduleReconnect("could not open the socket");
      return;
    }

    wsRef.current = ws;

    ws.onopen = () => {
      connectingRef.current = false;
      if (!mountedRef.current) {
        ws.close();
        return;
      }
      attemptRef.current = 0;
      setStatus("live");
      setDetail(null);

      clearTimers();
      heartbeatRef.current = setInterval(() => {
        if (ws.readyState === WebSocket.OPEN) {
          try {
            ws.send(JSON.stringify({ type: "ping" }));
          } catch {
            /* onclose will take over */
          }
        }
      }, HEARTBEAT_MS);
    };

    ws.onmessage = (ev) => {
      if (!mountedRef.current) return;
      try {
        const data = JSON.parse(ev.data);
        if (data.type === "hotlist_detection" && data.payload) {
          setLastEventAt(Date.now());
          cbRef.current(data.payload as AlertEvent);
        }
      } catch {
        /* ignore malformed frames and pongs */
      }
    };

    ws.onerror = () => {
      try {
        ws.close();
      } catch {
        /* already closing */
      }
    };

    ws.onclose = (event) => {
      if (heartbeatRef.current) {
        clearInterval(heartbeatRef.current);
        heartbeatRef.current = null;
      }
      connectingRef.current = false;
      // An auth close is recoverable: getValidAccessToken() refreshes on the next
      // attempt, so back off briefly instead of hammering the server.
      scheduleReconnect(describeClose(event));
    };
  }, [clearTimers, scheduleReconnect]);

  connectRef.current = connect;

  const reconnectNow = useCallback(() => {
    if (!mountedRef.current) return;
    attemptRef.current = 0;
    if (retryRef.current) {
      clearTimeout(retryRef.current);
      retryRef.current = null;
    }
    const ws = wsRef.current;
    wsRef.current = null;
    if (ws) {
      ws.onclose = null;
      ws.onerror = null;
      try {
        ws.close();
      } catch {
        /* already closed */
      }
    }
    connectRef.current();
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    connectRef.current();

    // A network change is exactly what this hook has to survive: the old socket
    // is dead by then, so rebuild immediately instead of waiting out the backoff.
    const onOnline = () => reconnectNow();
    window.addEventListener("online", onOnline);

    // Browsers throttle or freeze sockets in a background tab; on return the
    // connection is usually stale even though the socket still reports OPEN.
    const onVisibility = () => {
      if (document.visibilityState === "visible") reconnectNow();
    };
    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      mountedRef.current = false;
      window.removeEventListener("online", onOnline);
      document.removeEventListener("visibilitychange", onVisibility);
      clearTimers();

      const ws = wsRef.current;
      wsRef.current = null;
      if (ws) {
        // Detach the handlers before closing: some engines fire onclose
        // synchronously, and that must not schedule another reconnect.
        ws.onclose = null;
        ws.onerror = null;
        ws.onmessage = null;
        ws.onopen = null;
        try {
          ws.close();
        } catch {
          /* already closed */
        }
      }
    };
  }, [clearTimers, reconnectNow]);

  return { status, connected: status === "live", lastEventAt, detail, reconnectNow };
}