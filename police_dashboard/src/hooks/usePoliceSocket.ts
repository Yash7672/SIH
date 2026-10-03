import { useCallback, useEffect, useRef, useState } from "react";
import { AlertEvent, WS_BASE } from "../services/api";

export function usePoliceSocket(onAlert: (a: AlertEvent) => void) {
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const cbRef = useRef(onAlert);
  cbRef.current = onAlert;

  const connect = useCallback(() => {
    const token = localStorage.getItem("rakshak_token");
    if (!token) return;
    // WS_BASE comes from the same env value as the REST base URL (VITE_API_URL
    // or VITE_WS_URL), so the dashboard never points at the Docker hostname.
    const url = WS_BASE + "/api/v1/ws/police?token=" + token;
    try {
      const ws = new WebSocket(url);
      wsRef.current = ws;
      ws.onopen = () => setConnected(true);
      ws.onclose = () => {
        setConnected(false);
        setTimeout(connect, 3000);
      };
      ws.onerror = () => ws.close();
      ws.onmessage = (ev) => {
        try {
          const data = JSON.parse(ev.data);
          if (data.type === "hotlist_detection" && data.payload) {
            cbRef.current(data.payload as AlertEvent);
          }
        } catch {
          /* ignore malformed */
        }
      };
    } catch {
      setTimeout(connect, 3000);
    }
  }, []);

  useEffect(() => {
    connect();
    return () => {
      wsRef.current?.close();
    };
  }, [connect]);

  return { connected };
}
