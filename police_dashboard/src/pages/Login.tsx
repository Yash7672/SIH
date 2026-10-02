import { FormEvent, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Eye, EyeOff } from "lucide-react";
import { api, saveSession, TokenResponse } from "../services/api";
import { Wordmark } from "../components/Brand";
import { Button } from "../components/ui/Button";
import { Input } from "../components/ui/Input";
import { ThemeToggle } from "../components/ThemeToggle";

export default function Login() {
  const nav = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
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
      saveSession(data);
      nav("/");
    } catch (err: any) {
      setError(err.response?.data?.detail || "Login failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="relative flex min-h-screen items-center justify-center overflow-hidden bg-gradient-to-br from-primary-50 via-surface-bg to-accent-50 px-4 py-10">
      {/* Soft indigo-to-teal wash, the same palette as the citizen portal. */}
      <div
        className="pointer-events-none absolute inset-0 opacity-90"
        style={{
          background:
            "radial-gradient(60rem 40rem at 15% -10%, rgba(47,75,216,0.18), transparent 60%), radial-gradient(50rem 35rem at 110% 110%, rgba(6,182,212,0.16), transparent 60%)",
        }}
        aria-hidden
      />

      <div className="absolute right-4 top-4 z-10 sm:right-6 sm:top-6">
        <ThemeToggle />
      </div>
      <div className="relative w-full max-w-md">
        <div className="mb-6 flex flex-col items-center text-center">
          <Wordmark subtitle="Police console" />
          <h1 className="mt-5 text-2xl font-semibold tracking-tight text-surface-text">Police Console</h1>
          <p className="mt-1.5 text-sm text-surface-muted">
            Authorized personnel only. All activity is logged.
          </p>
        </div>

        <div className="rounded-2xl border border-surface-border bg-surface-card p-6 shadow-overlay sm:p-8">
          <form onSubmit={submit} className="space-y-5" noValidate>
            <Input
              label="Official email"
              type="email"
              autoComplete="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="cop@example.com"
            />

            <div className="relative">
              <Input
                label="Password"
                type={showPassword ? "text" : "password"}
                autoComplete="current-password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="••••••••"
                className="pr-11"
              />
              <button
                type="button"
                onClick={() => setShowPassword((v) => !v)}
                aria-label={showPassword ? "Hide password" : "Show password"}
                className="absolute right-3 top-[30px] rounded p-1 text-surface-muted transition-colors hover:text-surface-text"
              >
                {showPassword ? <EyeOff className="h-4 w-4" aria-hidden /> : <Eye className="h-4 w-4" aria-hidden />}
              </button>
            </div>

            {error ? (
              <div
                role="alert"
                className="rounded-lg border border-danger/40 bg-danger/10 px-3.5 py-2.5 text-sm font-medium text-danger"
              >
                {error}
              </div>
            ) : null}

            <Button type="submit" size="lg" fullWidth loading={loading}>
              {loading ? "Signing in…" : "Sign in"}
            </Button>
          </form>
        </div>

        <div className="mt-4 rounded-xl border border-surface-border bg-surface-card/70 px-4 py-3 text-center text-xs text-surface-muted">
          <p>
            Demo COP <code className="font-mono text-surface-text">cop@example.com</code> /{" "}
            <code className="font-mono text-surface-text">Police@123</code>
          </p>
          <p className="mt-1">
            Demo ADMIN <code className="font-mono text-surface-text">admin@example.com</code> /{" "}
            <code className="font-mono text-surface-text">Admin@123</code>
          </p>
        </div>
      </div>
    </div>
  );
}
