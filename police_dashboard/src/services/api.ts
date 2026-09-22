import axios from "axios";

export const API_BASE =
  (import.meta as any).env?.VITE_API_BASE_URL || "http://localhost:8000";

export const api = axios.create({
  baseURL: `${API_BASE}/api/v1`,
  timeout: 15000,
});

api.interceptors.request.use((config) => {
  const token = localStorage.getItem("rakshak_token");
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});

api.interceptors.response.use(
  (r) => r,
  (err) => {
    if (err.response?.status === 401) {
      localStorage.removeItem("rakshak_token");
      localStorage.removeItem("rakshak_user");
      if (window.location.pathname !== "/login") window.location.href = "/login";
    }
    return Promise.reject(err);
  }
);

export interface User {
  id: string;
  name: string;
  email: string;
  role: string;
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

export const HOTLIST_STATUS_COLORS: Record<string, string> = {
  ACTIVE: "bg-red-500/15 text-red-400 ring-red-500/30",
  FIR_CONFIRMED: "bg-orange-500/15 text-orange-400 ring-orange-500/30",
  RECOVERED: "bg-emerald-500/15 text-emerald-400 ring-emerald-500/30",
  CLOSED: "bg-slate-500/15 text-slate-400 ring-slate-500/30",
  EXPIRED: "bg-yellow-500/15 text-yellow-400 ring-yellow-500/30",
};

export const COMPLAINT_STATUS_COLORS: Record<string, string> = {
  PENDING: "bg-amber-500/15 text-amber-400 ring-amber-500/30",
  UNDER_REVIEW: "bg-blue-500/15 text-blue-400 ring-blue-500/30",
  VERIFIED: "bg-indigo-500/15 text-indigo-400 ring-indigo-500/30",
  REJECTED: "bg-red-500/15 text-red-400 ring-red-500/30",
  HOTLISTED: "bg-red-500/15 text-red-400 ring-red-500/30",
  CLOSED: "bg-slate-500/15 text-slate-400 ring-slate-500/30",
};
