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
  /** Which camera saw it. Both alert paths send it; older rows may not. */
  camera?: string;
  device_id?: string;
  last_seen_at?: string | null;
}

export interface Overview {
  active_hotlist: number;
  pending_complaints: number;
  detections_today: number;
  hotlist_matches_today: number;
  recovered_vehicles: number;
  active_devices: number;
}

export type HeatLayerName = "traffic" | "stolen";

export type HeatVehicleClass = "two_wheeler" | "car" | "bus" | "truck";

/**
 * One grid cell from GET /geo/heat.
 *
 * `w` means different things per layer: vehicles visible per processed frame
 * for `traffic`, and the time-decayed sighting count for `stolen`. Both are
 * normalised to 0..1 client-side against `max`, which is why the two can share
 * one renderer.
 */
export interface HeatCell {
  lat: number;
  lng: number;
  w: number;
  /** Frames or sightings behind this cell. */
  n: number;
}

export interface HeatResponse {
  cells: HeatCell[];
  max: number;
  min: number;
  /** Present on the traffic layer: rows are hour buckets. */
  bucket?: "hour";
  /** Present on the stolen layer: the decay constant that was applied. */
  tau_hours?: number;
  generated_at: string;
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
