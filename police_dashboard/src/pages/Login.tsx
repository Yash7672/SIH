import { FormEvent, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, TokenResponse } from "../services/api";

export default function Login() {
  const nav = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      const { data } = await api.post<TokenResponse>("/auth/login", { email, password });
      if (data.user.role !== "COP" && data.user.role !== "ADMIN") {
        setError("This console is for police. Citizens should use the Citizen Portal.");
        return;
      }
      localStorage.setItem("rakshak_token", data.access_token);
      localStorage.setItem("rakshak_user", JSON.stringify(data.user));
      nav("/");
    } catch (err: any) {
      setError(err.response?.data?.detail || "Login failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-slate-950 px-4">
      <div className="w-full max-w-md rounded-2xl bg-slate-900 p-8 shadow-2xl ring-1 ring-slate-800">
        <div className="mb-6 text-center">
          <div className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-xl bg-red-600 text-xl font-bold text-white">R</div>
          <h1 className="text-2xl font-bold text-white">RAKSHAK Police Console</h1>
          <p className="text-sm text-slate-400">Authorized personnel only</p>
        </div>
        <form onSubmit={submit} className="space-y-4">
          <input
            type="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="cop@example.com"
            className="w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2.5 text-sm text-white placeholder-slate-500 focus:border-red-500 focus:outline-none focus:ring-2 focus:ring-red-500/30"
          />
          <input
            type="password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="Password"
            className="w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2.5 text-sm text-white placeholder-slate-500 focus:border-red-500 focus:outline-none focus:ring-2 focus:ring-red-500/30"
          />
          {error && (
            <div className="rounded-lg bg-red-500/10 px-3 py-2 text-sm text-red-400 ring-1 ring-red-500/30">{error}</div>
          )}
          <button
            type="submit"
            disabled={loading}
            className="w-full rounded-lg bg-red-600 px-4 py-2.5 text-sm font-semibold text-white hover:bg-red-700 disabled:opacity-50"
          >
            {loading ? "Signing in…" : "Sign in"}
          </button>
        </form>
        <div className="mt-4 rounded-lg bg-slate-800 p-3 text-xs text-slate-400">
          Demo COP: <code className="font-mono text-slate-300">cop@example.com</code> / <code className="font-mono text-slate-300">Police@123</code>
          <br />
          Demo ADMIN: <code className="font-mono text-slate-300">admin@example.com</code> / <code className="font-mono text-slate-300">Admin@123</code>
        </div>
      </div>
    </div>
  );
}
