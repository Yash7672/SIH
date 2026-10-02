import { useEffect, useRef, useState } from "react";
import { Link, NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { ChevronDown, FilePlus2, LayoutDashboard, LogOut, Menu, X } from "lucide-react";
import { clearSession } from "../services/api";
import { Wordmark } from "./Brand";
import { ThemeModePicker, ThemeToggle } from "./ThemeToggle";

const NAV = [
  { to: "/", label: "Dashboard", icon: LayoutDashboard, end: true },
  { to: "/complaints/new", label: "Report", icon: FilePlus2, end: false },
  { to: "/complaints", label: "My complaints", icon: null, end: false },
];

export default function Layout() {
  const nav = useNavigate();
  const location = useLocation();
  const [menuOpen, setMenuOpen] = useState(false);
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  const user = JSON.parse(localStorage.getItem("rakshak_user") || "null");
  const initials = user?.name
    ? String(user.name)
        .split(" ")
        .slice(0, 2)
        .map((p: string) => p[0])
        .join("")
        .toUpperCase()
    : "?";

  // Close both menus on navigation so the mobile drawer never covers a new page.
  useEffect(() => {
    setMenuOpen(false);
    setMobileNavOpen(false);
  }, [location.pathname]);

  // Outside-click + Escape close the user menu.
  useEffect(() => {
    if (!menuOpen) return;
    const onClick = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setMenuOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setMenuOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [menuOpen]);

  function logout() {
    clearSession();
    nav("/login");
  }

  const linkClass = ({ isActive }: { isActive: boolean }) =>
    [
      "inline-flex items-center gap-1.5 rounded-lg px-3 py-2 text-sm font-medium transition-colors",
      isActive
        ? "bg-primary-50 text-primary-700"
        : "text-surface-muted hover:bg-surface-raised hover:text-surface-text",
    ].join(" ");

  return (
    <div className="flex min-h-screen flex-col">
      <header className="sticky top-0 z-40 border-b border-surface-border bg-surface-card/90 backdrop-blur">
        <div className="mx-auto flex w-full max-w-6xl items-center justify-between gap-4 px-4 py-3">
          <Link to="/" className="rounded-lg" aria-label="RAKSHAK home">
            <Wordmark subtitle="Citizen portal" />
          </Link>

          <nav className="hidden items-center gap-1 md:flex" aria-label="Main">
            {NAV.map((item) => (
              <NavLink key={item.to} to={item.to} end={item.end} className={linkClass}>
                {item.label}
              </NavLink>
            ))}
          </nav>

          <div className="flex items-center gap-2">
            <ThemeToggle />

            {user ? (
              <div className="relative" ref={menuRef}>
                <button
                  type="button"
                  onClick={() => setMenuOpen((v) => !v)}
                  aria-haspopup="menu"
                  aria-expanded={menuOpen}
                  className="inline-flex items-center gap-2 rounded-lg px-1.5 py-1.5 transition-colors hover:bg-surface-raised"
                >
                  <span className="flex h-8 w-8 items-center justify-center rounded-full bg-solid text-xs font-semibold text-white">
                    {initials}
                  </span>
                  <ChevronDown className="h-4 w-4 text-surface-muted" aria-hidden />
                </button>

                {menuOpen ? (
                  <div
                    role="menu"
                    className="absolute right-0 mt-2 w-60 animate-slide-up rounded-xl border border-surface-border bg-surface-card p-1.5 shadow-overlay"
                  >
                    <div className="border-b border-surface-border px-3 py-2.5">
                      <p className="truncate text-sm font-medium text-surface-text">{user.name}</p>
                      <p className="truncate text-xs text-surface-muted">{user.email}</p>
                    </div>
                    <div className="px-2 py-2">
                      <ThemeModePicker className="w-full" />
                    </div>
                    <button
                      type="button"
                      role="menuitem"
                      onClick={logout}
                      className="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left text-sm font-medium text-danger transition-colors hover:bg-danger/10"
                    >
                      <LogOut className="h-4 w-4" aria-hidden /> Log out
                    </button>
                  </div>
                ) : null}
              </div>
            ) : (
              <Link
                to="/login"
                className="rounded-lg bg-solid px-3 py-2 text-sm font-medium text-white transition-colors hover:bg-solid-hover"
              >
                Sign in
              </Link>
            )}

            <button
              type="button"
              onClick={() => setMobileNavOpen((v) => !v)}
              aria-label="Toggle navigation"
              aria-expanded={mobileNavOpen}
              className="inline-flex h-9 w-9 items-center justify-center rounded-lg text-surface-muted transition-colors hover:bg-surface-raised md:hidden"
            >
              {mobileNavOpen ? <X className="h-5 w-5" aria-hidden /> : <Menu className="h-5 w-5" aria-hidden />}
            </button>
          </div>
        </div>

        {mobileNavOpen ? (
          <nav className="border-t border-surface-border bg-surface-card px-4 py-2 md:hidden" aria-label="Mobile">
            {NAV.map((item) => (
              <NavLink key={item.to} to={item.to} end={item.end} className={linkClass}>
                {item.label}
              </NavLink>
            ))}
            <div className="px-3 py-2">
              <ThemeModePicker className="w-full" />
            </div>
            <button
              type="button"
              onClick={() => {
                setMobileNavOpen(false);
                logout();
              }}
              className="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left text-sm font-medium text-danger transition-colors hover:bg-danger/10"
            >
              <LogOut className="h-4 w-4" aria-hidden /> Log out
            </button>
          </nav>
        ) : null}
      </header>

      <main className="mx-auto w-full max-w-6xl flex-1 px-4 py-6 sm:py-8">
        <Outlet />
      </main>

      <footer className="border-t border-surface-border bg-surface-card">
        <div className="mx-auto w-full max-w-6xl px-4 py-5 text-center text-xs text-surface-muted">
          RAKSHAK · Privacy-first crowdsourced ANPR · Plate photos and locations are encrypted in transit.
        </div>
      </footer>
    </div>
  );
}
