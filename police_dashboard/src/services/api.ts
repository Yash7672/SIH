import axios from "axios";

const env = (import.meta as any).env || {};

function trimmed(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

/**
 * Where the backend lives, decided at RUNTIME instead of baked into the bundle.
 *
 * A saved LAN IP is the single reason this dashboard breaks after a network
 * change: Vite inlines VITE_* into the JS at build time, so a bundle built for
 * 10.175.26.85 keeps dialling that address after the router hands out a new one,
 * and the live feed silently stops. Deriving the host from `window.location`
 * means the page always talks to the backend on the same machine, whatever the
 * IP is now - no rebuild, no restart.
 *
 * An explicit VITE_API_URL still wins, so container/CI setups can pin a host.
 */
export function resolveApiBase(): string {
  const explicit = trimmed(env.VITE_API_URL) || trimmed(env.VITE_API_BASE_URL);
  if (explicit) return explicit.replace(/\/+$/, "");

  if (typeof window !== "undefined" && window.location && window.location.hostname) {
    // http://10.175.26.85:5174 -> http://10.175.26.85:8000
    return `${window.location.protocol}//${window.location.hostname}:8000`;
  }
  return "http://localhost:8000";
}

/** Same derivation for the socket: https becomes wss, http becomes ws. */
export function resolveWsBase(apiBase: string): string {
  const explicit = trimmed(env.VITE_WS_URL);
  const source = explicit || apiBase;
  return source.replace(/^http/, "ws").replace(/\/+$/, "");
}

export const API_BASE: string = resolveApiBase();
export const WS_BASE: string = resolveWsBase(API_BASE);

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

export async function refreshAccessToken(): Promise<string> {
  const refreshToken = localStorage.getItem(REFRESH_KEY);
  if (!refreshToken) throw new Error("no refresh token");
  const { data } = await axios.post<TokenResponse>(`${API_BASE}/api/v1/auth/refresh`, {
    refresh_token: refreshToken,
  });
  saveSession(data);
  return data.access_token;
}

/**
 * Read a JWT's `exp` claim without verifying it.
 * Used only to decide *when* to refresh proactively; the server remains the
 * authority on validity.
 */
function tokenExpiresAt(token: string): number | null {
  const parts = token.split(".");
  if (parts.length < 2) return null;
  try {
    const payload = JSON.parse(atob(parts[1].replace(/-/g, "+").replace(/_/g, "/")));
    return typeof payload.exp === "number" ? payload.exp * 1000 : null;
  } catch {
    return null;
  }
}

/**
 * A token that is valid *right now*, refreshing proactively when it is expired
 * or about to expire.
 *
 * The alert socket needs this: it authenticates at connect time, and access
 * tokens only live 30 minutes. A dashboard left open across that boundary used
 * to reconnect forever with a dead token and never recover.
 */
export async function getValidAccessToken(skewMs = 60000): Promise<string | null> {
  const token = localStorage.getItem(TOKEN_KEY);
  if (!token) return null;

  const expiresAt = tokenExpiresAt(token);
  if (expiresAt !== null && expiresAt - Date.now() > skewMs) return token;

  try {
    return await (refreshInFlight = refreshInFlight || refreshAccessToken()).finally(() => {
      refreshInFlight = null;
    });
  } catch {
    // A dead refresh token means the session is gone; clearing it stops the
    // socket from reconnecting in a loop and lets the UI send the user to login.
    clearSession();
    return null;
  }
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

/**
 * Recent hotlist detections, newest first.
 *
 * The alert list used to live only in memory, so a browser refresh emptied it
 * and the operator had no way to see what they had already missed. Hydrating
 * from GET /alerts on load makes the page survivable; the socket then keeps it
 * up to date. De-duplication against live events happens in the merge helper.
 */
export async function fetchRecentAlerts(): Promise<AlertEvent[]> {
  // No params: GET /alerts returns the 50 most recent hotlist detections from the
  // last 24 hours, which is the window the dashboard shows.
  const { data } = await api.get<AlertEvent[]>("/alerts");
  return Array.isArray(data) ? data : [];
}

/** Stable identity for an alert, used to merge history with live events. */
export function alertKey(a: Pick<AlertEvent, "sighting_id" | "plate" | "timestamp">): string {
  return a.sighting_id || `${a.plate}::${a.timestamp}`;
}

/**
 * Merge freshly-fetched history into the live list.
 *
 * Newest first, de-duplicated by sighting id, capped. A live event that is
 * already present in the history must not appear twice, which is what happens on
 * every reconnect because the backend keeps sending detections the socket missed
 * while the page was closed.
 */
export function mergeAlerts(live: AlertEvent[], history: AlertEvent[], cap = 100): AlertEvent[] {
  const seen = new Set<string>();
  const out: AlertEvent[] = [];

  for (const item of [...live, ...history]) {
    if (!item || !item.plate) continue;
    const key = alertKey(item);
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(item);
  }

  out.sort((a, b) => {
    const at = Date.parse(a.timestamp || "") || 0;
    const bt = Date.parse(b.timestamp || "") || 0;
    return bt - at;
  });

  return out.slice(0, cap);
}
