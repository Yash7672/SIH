import { FormEvent, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, TokenResponse } from "../services/api";

export default function Register() {
  const nav = useNavigate();
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [phone, setPhone] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      const { data } = await api.post<TokenResponse>("/auth/register", {
        name,
        email,
        phone: phone || undefined,
        password,
        role: "CITIZEN",
      });
      localStorage.setItem("rakshak_token", data.access_token);
      localStorage.setItem("rakshak_user", JSON.stringify(data.user));
      nav("/");
    } catch (err: any) {
      setError(err.response?.data?.detail || "Registration failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-gradient-to-br from-brand-50 to-slate-100 px-4">
      <div className="w-full max-w-md rounded-2xl bg-white p-8 shadow-xl ring-1 ring-slate-200">
        <h1 className="mb-1 text-center text-2xl font-bold text-slate-900">Create account</h1>
        <p className="mb-6 text-center text-sm text-slate-500">Citizen registration</p>
        <form onSubmit={submit} className="space-y-4">
          <input required value={name} onChange={(e) => setName(e.target.value)} placeholder="Full name"
            className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-200" />
          <input required type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="Email"
            className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-200" />
          <input value={phone} onChange={(e) => setPhone(e.target.value)} placeholder="Phone (optional)"
            className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-200" />
          <input required type="password" minLength={8} value={password} onChange={(e) => setPassword(e.target.value)} placeholder="Password (min 8 chars)"
            className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-200" />
          {error && <div className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700 ring-1 ring-red-200">{error}</div>}
          <button disabled={loading} type="submit"
            className="w-full rounded-lg bg-brand-600 px-4 py-2.5 text-sm font-semibold text-white hover:bg-brand-700 disabled:opacity-50">
            {loading ? "Creating…" : "Create account"}
          </button>
        </form>
        <p className="mt-4 text-center text-sm text-slate-500">
          Already registered? <Link to="/login" className="font-medium text-brand-600 hover:underline">Sign in</Link>
        </p>
      </div>
    </div>
  );
}
