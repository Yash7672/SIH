import { useEffect, useMemo, useRef, useState } from "react";
import { Link, NavLink, Outlet, useNavigate } from "react-router-dom";
import {
  BarChart3,
  ChevronDown,
  ChevronLeft,
  LayoutDashboard,
  ListChecks,
  LogOut,
  PanelLeft,
  Radio,
  Search,
  Settings,
  ShieldAlert,
  Siren,
  X,
} from "lucide-react";
import { AlertEvent, clearSession } from "../services/api";
import { usePoliceSocket, SocketStatus } from "../hooks/usePoliceSocket";
import { SocketStateProvider } from "../hooks/SocketState";
import { Wordmark } from "./Brand";
import { Button } from "./ui/Button";
import { formatCoords, platePath, timeAgo } from "../lib/format";
import { ThemeModePicker, ThemeToggle } from "./ThemeToggle";
import { PageBackground } from "./ui/PageBackground";

const NAV = [
  { to: "/", label: "Overview", icon: LayoutDashboard, end: true },
  { to: "/alerts", label: "Live Alerts", icon: Siren, end: false, badge: "alerts" },
  { to: "/complaints", label: "Complaints", icon: ListChecks, end: false },
  { to: "/hotlist", label: "Hotlist", icon: ShieldAlert, end: false },
  { to: "/search", label: "Vehicle Search", icon: Search, end: false },
  { to: "/analytics", label: "Analytics", icon: BarChart3, end: false },
] as const;

/**
 * Live / Reconnecting / Disconnected, with the reason in the tooltip.
 *
 * The previous pill was a two-state light. A socket cycling through its backoff
 * rendered as "Disconnected", which reads like a hard failure and hides the fact
 * that recovery is already under way.
 */
export const SOCKET_LABELS: Record<SocketStatus, string> = {
  connecting: "Connecting",
  live: "Live",
  reconnecting: "Reconnecting",
  offline: "Disconnected",
};

function ConnectionPill({
  status,
  detail,
  onRetry,
}: {
  status: SocketStatus;
  detail: string | null;
  onRetry: () => void;
}) {
  const connected = status === "live";
  const tone = connected
    ? "bg-success/10 text-success ring-success/30"
    : status === "reconnecting" || status === "connecting"
      ? "bg-warning/10 text-warning ring-warning/30"
      : "bg-danger/10 text-danger ring-danger/30";
  const dot = connected ? "bg-success" : status === "offline" ? "bg-danger" : "bg-warning";
  const title = detail ? `${SOCKET_LABELS[status]} - ${detail}` : SOCKET_LABELS[status];

  return (
    <button
      type="button"
      onClick={onRetry}
      title={`${title} (click to reconnect now)`}
      className={[
        "inline-flex items-center gap-2 rounded-full px-3 py-1.5 text-xs font-medium ring-1 ring-inset",
        tone,
        "cursor-pointer",
      ].join(" ")}
    >
      <span className="relative flex h-2 w-2">
        {connected ? (
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-success opacity-60" />
        ) : null}
        <span className={["relative inline-flex h-2 w-2 rounded-full", dot].join(" ")} />
      </span>
      {SOCKET_LABELS[status]}
    </button>
  );
}

export default function Layout({
  alerts,
  onAlert,
}: {
  alerts: AlertEvent[];
  onAlert: (a: AlertEvent) => void;
}) {
  const nav = useNavigate();
  const user = JSON.parse(localStorage.getItem("rakshak_user") || "null");
  const socket = usePoliceSocket(onAlert);
  const { status, connected, detail, reconnectNow } = socket;

  const [drawerOpen, setDrawerOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [banner, setBanner] = useState<AlertEvent | null>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  const latestId = alerts[0]?.sighting_id ?? null;

  useEffect(() => {
    if (!latestId) return;
    setBanner(alerts[0]);
    const t = setTimeout(() => setBanner(null), 8000);
    return () => clearTimeout(t);
    // Keyed on the newest sighting only: a re-render of the same alert list
    // must not restart the 8-second timer.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [latestId]);

  useEffect(() => {
    if (!menuOpen) return;
    const onClick = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setMenuOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setMenuOpen(false);
    document.addEventListener("mousedown", onClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [menuOpen]);

  const items = useMemo(
    () => (user?.role === "ADMIN" ? [...NAV, { to: "/admin", label: "Admin", icon: Settings, end: false }] : NAV),
    [user?.role]
  );

  const sidebarWidth = collapsed ? "lg:w-[68px]" : "lg:w-60";
  const linkBase =
    "flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors";

  function navLinkClass(isActive: boolean) {
    return [
      linkBase,
      isActive
        ? "bg-solid text-white shadow-sm"
        : "text-dark-600 hover:bg-dark-700 hover:text-white",
    ].join(" ");
  }

  return (
    <PageBackground>
      {/* One socket for the whole console; pages read its state from here rather
          than opening their own (see hooks/SocketState.tsx). */}
      <SocketStateProvider value={socket}>
      <div className="min-h-screen">
        {/* Mobile drawer backdrop */}
        {drawerOpen ? (
          <button
            type="button"
            aria-label="Close navigation"
            onClick={() => setDrawerOpen(false)}
            className="fixed inset-0 z-40 bg-primary-900/60 backdrop-blur-sm lg:hidden"
          />
        ) : null}

        <div className="flex min-h-screen">
          <aside
            className={[
              "fixed inset-y-0 left-0 z-50 flex shrink-0 flex-col border-r border-dark-700 bg-dark-900 transition-all duration-200",
              "w-60 lg:sticky lg:top-0 lg:h-screen lg:translate-x-0",
              sidebarWidth,
              collapsed ? "lg:w-[68px]" : "",
              drawerOpen ? "translate-x-0" : "-translate-x-full",
            ].join(" ")}
          >
            <div className="flex items-center justify-between gap-2 border-b border-dark-700 px-4 py-4">
              <Link to="/" onClick={() => setDrawerOpen(false)} className="rounded-lg">
                <Wordmark
                  tone="dark"
                  subtitle="Police console"
                  className={collapsed ? "lg:hidden" : ""}
                />
              </Link>
              <button
                type="button"
                onClick={() => setDrawerOpen(false)}
                aria-label="Close navigation"
                className="rounded-md p-1.5 text-dark-600 hover:bg-dark-700 hover:text-white lg:hidden"
              >
                <X className="h-4 w-4" aria-hidden />
              </button>
            </div>

            <nav className="scrollbar-dark flex-1 space-y-1 overflow-y-auto px-2 py-4" aria-label="Main">
              {items.map((n) => {
                const Icon = n.icon;
                const count = "badge" in n && n.badge === "alerts" ? alerts.length : 0;
                return (
                  <NavLink
                    key={n.to}
                    to={n.to}
                    end={n.end}
                    title={n.label}
                    onClick={() => setDrawerOpen(false)}
                    className={({ isActive }) => navLinkClass(isActive)}
                  >
                    <Icon className="h-[18px] w-[18px] shrink-0" aria-hidden />
                    <span className={collapsed ? "lg:hidden" : ""}>{n.label}</span>
                    {count > 0 ? (
                      <span
                        className={[
                          "ml-auto rounded-full bg-solid-danger px-1.5 py-0.5 text-[10px] font-bold text-white",
                          collapsed ? "lg:hidden" : "",
                        ].join(" ")}
                      >
                        {count}
                      </span>
                    ) : null}
                  </NavLink>
                );
              })}
            </nav>

            <div className="border-t border-dark-700 px-3 py-3">
              <div className={["flex items-center gap-2", collapsed ? "lg:justify-center" : ""].join(" ")}>
                <span
                  className={[
                    "h-2 w-2 shrink-0 rounded-full",
                    connected ? "bg-success" : status === "offline" ? "bg-danger" : "bg-warning",
                  ].join(" ")}
                  aria-hidden
                />
                <span className={["text-xs", collapsed ? "lg:hidden" : "text-dark-600"].join(" ")}>
                  {SOCKET_LABELS[status]}
                  {detail && !collapsed ? `: ${detail}` : ""}
                </span>
              </div>
              <button
                type="button"
                onClick={() => setCollapsed((v) => !v)}
                className={[
                  "mt-3 hidden w-full items-center gap-2 rounded-md px-2 py-1.5 text-xs text-dark-600 hover:bg-dark-700 hover:text-white lg:flex",
                  collapsed ? "justify-center" : "",
                ].join(" ")}
                aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
              >
                <ChevronLeft
                  className={["h-4 w-4 transition-transform", collapsed ? "rotate-180" : ""].join(" ")}
                  aria-hidden
                />
                {collapsed ? null : <span>Collapse</span>}
              </button>
            </div>
          </aside>

          <div className="flex min-w-0 flex-1 flex-col">
            <header className="sticky top-0 z-30 border-b border-surface-border bg-surface-card/90 backdrop-blur">
              <div className="flex items-center gap-3 px-4 py-3 sm:px-6">
                <button
                  type="button"
                  onClick={() => setDrawerOpen(true)}
                  aria-label="Open navigation"
                  className="inline-flex h-9 w-9 items-center justify-center rounded-lg text-surface-muted transition-colors hover:bg-surface-raised lg:hidden"
                >
                  <PanelLeft className="h-5 w-5" aria-hidden />
                </button>

                <div className="hidden items-center gap-2 text-sm text-surface-muted sm:flex">
                  <Radio className="h-4 w-4 text-accent-600" aria-hidden />
                  Hotlist detection stream
                </div>

                <div className="ml-auto flex items-center gap-3">
                  <ConnectionPill status={status} detail={detail} onRetry={reconnectNow} />
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
                        <span className="flex h-8 w-8 items-center justify-center rounded-full bg-dark-900 text-xs font-semibold text-white">
                          {String(user.name || "?")
                            .split(" ")
                            .slice(0, 2)
                            .map((p: string) => p[0])
                            .join("")
                            .toUpperCase()}
                        </span>
                        <span className="hidden text-sm font-medium text-surface-text sm:inline">
                          {user.name}
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
                            <p className="mt-1 inline-flex rounded-full chip-primary px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide">
                              {user.role}
                            </p>
                          </div>
                          <div className="px-2 py-2">
                            <ThemeModePicker className="w-full" />
                          </div>
                          <button
                            type="button"
                            role="menuitem"
                            onClick={() => {
                              clearSession();
                              nav("/login");
                            }}
                            className="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left text-sm font-medium text-danger transition-colors hover:bg-danger/10"
                          >
                            <LogOut className="h-4 w-4" aria-hidden /> Sign out
                          </button>
                        </div>
                      ) : null}
                    </div>
                  ) : null}
                </div>
              </div>
            </header>

            {banner ? (
              <div
                role="alert"
                className="flex animate-slide-up flex-wrap items-center justify-between gap-3 bg-solid-danger px-4 py-3 text-white sm:px-6"
              >
                <div className="flex min-w-0 items-center gap-3">
                  <Siren className="h-5 w-5 shrink-0" aria-hidden />
                  <div className="min-w-0">
                    <p className="text-sm font-bold">HOTLIST VEHICLE DETECTED</p>
                    <p className="truncate text-xs text-white/85">
                      {banner.plate} · {formatCoords(banner.latitude, banner.longitude)} ·{" "}
                      {timeAgo(banner.timestamp)}
                      {banner.confidence ? ` · ${Math.round(banner.confidence * 100)}%` : ""}
                    </p>
                  </div>
                </div>
                <Link to={platePath(banner.plate)} onClick={() => setBanner(null)}>
                  <Button size="sm" className="bg-surface-card text-danger hover:bg-danger/10">
                    View vehicle
                  </Button>
                </Link>
              </div>
            ) : null}

            <main className="flex-1 px-4 py-6 sm:px-6">
              <Outlet />
            </main>

            <footer className="border-t border-surface-border px-4 py-4 text-center text-xs text-surface-muted sm:px-6">
              RAKSHAK Police Console · Internal system · Alerts are advisory, verify before intercepting.
            </footer>
          </div>
        </div>
      </div>
      </SocketStateProvider>
    </PageBackground>
  );
}
