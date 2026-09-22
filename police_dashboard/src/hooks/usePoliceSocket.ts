import { useCallback, useEffect, useRef, useState } from "react";
import { API_BASE, AlertEvent } from "../services/api";

export function usePoliceSocket(onAlert: (a: AlertEvent) => void) {
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const cbRef = useRef(onAlert);
  cbRef.current = onAlert;

  const connect = useCallback(() => {
    const token = localStorage.getItem("rakshak_token");
    if (!token) return;
    const url = API_BASE.replace(/^http/, "ws") + "/api/v1/ws/police?token=" + token;
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
