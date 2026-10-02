import axios from "axios";

const env = (import.meta as any).env || {};

/**
 * Host used by the *browser*, not by a container. `scripts/start.ps1` writes
 * `police_dashboard/.env.local` with the PC's LAN IP (docker compose passes it
 * as a runtime env var), so the same bundle is reachable from the PC and from a
 * phone on the same Wi-Fi. `VITE_API_BASE_URL` is kept as a legacy alias.
 */
export const API_BASE: string = env.VITE_API_URL || env.VITE_API_BASE_URL || "http://localhost:8000";

/** Alert socket. Explicit VITE_WS_URL wins, otherwise derived from API_BASE. */
export const WS_BASE: string = String(env.VITE_WS_URL || API_BASE).replace(/^http/, "ws");

export const api = axios.create({
  baseURL: `${API_BASE}/api/v1`,
  timeout: 15000,
});

const TOKEN_KEY = "rakshak_token";
const REFRESH_KEY = "rakshak_refresh";
const USER_KEY = "rakshak_user";

/** Store the access + refresh pair from a login/register/refresh response. */
export function saveSession(data: TokenResponse) {
  localStorage.setItem(TOKEN_KEY, data.access_token);
  if (data.refresh_token) localStorage.setItem(REFRESH_KEY, data.refresh_token);
  localStorage.setItem(USER_KEY, JSON.stringify(data.user));
}

export function clearSession() {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(REFRESH_KEY);
  localStorage.removeItem(USER_KEY);
}

api.interceptors.request.use((config) => {
  const token = localStorage.getItem(TOKEN_KEY);
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});

// A single in-flight refresh is shared by every request that hits a 401 at the
// same time, so a batch of requests only renews the token once.
let refreshInFlight: Promise<string> | null = null;

async function refreshAccessToken(): Promise<string> {
  const refreshToken = localStorage.getItem(REFRESH_KEY);
  if (!refreshToken) throw new Error("no refresh token");
  const { data } = await axios.post<TokenResponse>(`${API_BASE}/api/v1/auth/refresh`, {
    refresh_token: refreshToken,
  });
  saveSession(data);
  return data.access_token;
}

function forceLogin() {
  clearSession();
  if (window.location.pathname !== "/login") window.location.href = "/login";
}

api.interceptors.response.use(
  (r) => r,
  async (err) => {
    const original = err.config;
    const isAuthCall = typeof original?.url === "string" && original.url.includes("/auth/");

    if (err.response?.status === 401 && original && !original._retried && !isAuthCall) {
      original._retried = true;
      try {
        refreshInFlight = refreshInFlight || refreshAccessToken();
        const token = await refreshInFlight.finally(() => {
          refreshInFlight = null;
        });
        original.headers = { ...(original.headers || {}), Authorization: `Bearer ${token}` };
        return api(original);
      } catch {
        forceLogin();
        return Promise.reject(err);
      }
    }

    if (err.response?.status === 401) forceLogin();
    return Promise.reject(err);
  }
);

export interface User {
  id: string;
  name: string;
  email: string;
  role: string;
  phone?: string;
  created_at?: string;
}

export interface TokenResponse {
  access_token: string;
  refresh_token: string;
  user: User;
}

export interface HotlistEntry {
  id: string;
  plate: string;
  complaint_id?: string;
  status: string;
  added_at: string;
  expiry_at?: string;
  fir_reference?: string;
  last_seen_at?: string;
  last_seen_lat?: number;
  last_seen_lng?: number;
}

export interface Complaint {
  id: string;
  plate: string;
  complaint_type: string;
  description?: string;
  status: string;
  created_at: string;
  updated_at: string;
}

export interface Sighting {
  id: string;
  hotlist_id: string;
  device_id: string;
  latitude: number;
  longitude: number;
  detected_at: string;
  confidence?: number;
}

export interface AlertEvent {
  sighting_id: string;
  plate: string;
  latitude: number;
  longitude: number;
  timestamp: string;
  confidence?: number;
  hotlist_id: string;
}

export interface Overview {
  active_hotlist: number;
  pending_complaints: number;
  detections_today: number;
  hotlist_matches_today: number;
  recovered_vehicles: number;
  active_devices: number;
}

/* Legacy dark-console pill classes, kept so any straggler call site still
   resolves. New code should prefer the chip-* classes from
   components/ui/StatusChip.tsx and components/ui/HotlistChip.tsx, which are
   theme-aware. */
export const HOTLIST_STATUS_COLORS: Record<string, string> = {
  ACTIVE: "chip-danger",
  FIR_CONFIRMED: "chip-warning",
  RECOVERED: "chip-success",
  CLOSED: "chip-neutral",
  EXPIRED: "chip-warning",
};

export const COMPLAINT_STATUS_COLORS: Record<string, string> = {
  PENDING: "chip-warning",
  UNDER_REVIEW: "chip-info",
  VERIFIED: "chip-primary",
  REJECTED: "chip-danger",
  HOTLISTED: "chip-danger",
  CLOSED: "chip-neutral",
};
