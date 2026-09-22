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
    }
    return Promise.reject(err);
  }
);

export interface User {
  id: string;
  name: string;
  email: string;
  phone?: string;
  role: string;
  created_at: string;
}

export interface TokenResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
  user: User;
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

export const STATUS_COLORS: Record<string, string> = {
  PENDING: "bg-amber-100 text-amber-800",
  UNDER_REVIEW: "bg-blue-100 text-blue-800",
  VERIFIED: "bg-indigo-100 text-indigo-800",
  REJECTED: "bg-red-100 text-red-800",
  HOTLISTED: "bg-red-100 text-red-800",
  CLOSED: "bg-slate-100 text-slate-700",
};
