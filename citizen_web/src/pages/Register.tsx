import { FormEvent, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, saveSession, TokenResponse } from "../services/api";
import { Wordmark } from "../components/Brand";
import { Button } from "../components/ui/Button";
import { Input } from "../components/ui/Input";
import { ThemeToggle } from "../components/ThemeToggle";

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
      saveSession(data);
      nav("/");
    } catch (err: any) {
      setError(err.response?.data?.detail || "Registration failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="relative flex min-h-screen items-center justify-center bg-gradient-to-br from-primary-50 via-surface-bg to-accent-50 px-4 py-10">
      <div className="absolute right-4 top-4 z-10 sm:right-6 sm:top-6">
        <ThemeToggle />
      </div>

      <div className="w-full max-w-md">
        <div className="mb-6 flex flex-col items-center text-center">
          <Wordmark />
          <h1 className="mt-5 text-2xl font-semibold tracking-tight text-surface-text">
            Create your account
          </h1>
          <p className="mt-1.5 text-sm text-surface-muted">
            File complaints and follow your vehicle&apos;s status.
          </p>
        </div>

        <div className="rounded-2xl border border-surface-border bg-surface-card p-6 shadow-overlay sm:p-8">
          <form onSubmit={submit} className="space-y-5" noValidate>
            <Input
              label="Full name"
              required
              autoComplete="name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Your name"
            />
            <Input
              label="Email"
              type="email"
              required
              autoComplete="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="citizen@example.com"
            />
            <Input
              label="Phone"
              type="tel"
              autoComplete="tel"
              value={phone}
              onChange={(e) => setPhone(e.target.value)}
              placeholder="Optional"
              hint="Used by police only if they need to reach you about a complaint."
            />
            <Input
              label="Password"
              type="password"
              required
              minLength={8}
              autoComplete="new-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="At least 8 characters"
              hint={password && password.length < 8 ? `${8 - password.length} more character(s) needed.` : undefined}
            />

            {error ? (
              <div
                role="alert"
                className="rounded-lg border border-danger/40 bg-danger/10 px-3.5 py-2.5 text-sm font-medium text-danger"
              >
                {error}
              </div>
            ) : null}

            <Button type="submit" size="lg" fullWidth loading={loading}>
              {loading ? "Creating account…" : "Create account"}
            </Button>
          </form>

          <p className="mt-5 text-center text-sm text-surface-muted">
            Already registered?{" "}
            <Link to="/login" className="font-medium text-primary-600 hover:text-primary-700 hover:underline">
              Sign in
            </Link>
          </p>
        </div>
      </div>
    </div>
  );
}
