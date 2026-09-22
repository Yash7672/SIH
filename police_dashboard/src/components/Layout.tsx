import { useEffect, useState } from "react";
import { Link, NavLink, Outlet, useNavigate } from "react-router-dom";
import { AlertEvent } from "../services/api";
import { usePoliceSocket } from "../hooks/usePoliceSocket";

const NAV = [
  { to: "/", label: "Overview", end: true },
  { to: "/complaints", label: "Complaints" },
  { to: "/hotlist", label: "Hotlist" },
  { to: "/alerts", label: "Live Alerts" },
  { to: "/search", label: "Vehicle Search" },
  { to: "/analytics", label: "Analytics" },
];

export default function Layout({
  alerts,
  onAlert,
}: {
  alerts: AlertEvent[];
  onAlert: (a: AlertEvent) => void;
}) {
  const nav = useNavigate();
  const user = JSON.parse(localStorage.getItem("rakshak_user") || "null");
  const { connected } = usePoliceSocket(onAlert);
  const [banner, setBanner] = useState<AlertEvent | null>(null);

  useEffect(() => {
    if (alerts.length > 0) {
      setBanner(alerts[0]);
      const t = setTimeout(() => setBanner(null), 8000);
      return () => clearTimeout(t);
    }
  }, [alerts[0]?.sighting_id]);

  return (
    <div className="min-h-screen flex">
      <aside className="w-60 shrink-0 border-r border-slate-800 bg-slate-900/50 flex flex-col">
        <div className="px-4 py-5 border-b border-slate-800">
          <div className="flex items-center gap-2">
            <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-red-600 text-sm font-bold text-white">R</span>
            <div>
              <p className="text-sm font-bold">RAKSHAK</p>
              <p className="text-[10px] uppercase tracking-wider text-slate-500">Police Console</p>
            </div>
          </div>
        </div>
        <nav className="flex-1 px-2 py-4 space-y-1">
          {NAV.map((n) => (
            <NavLink
              key={n.to}
              to={n.to}
              end={n.end}
              className={({ isActive }) =>
                `block rounded-lg px-3 py-2 text-sm font-medium ${
                  isActive ? "bg-brand-600 text-white" : "text-slate-300 hover:bg-slate-800 hover:text-white"
                }`
              }
            >
              {n.label}
              {n.to === "/alerts" && alerts.length > 0 && (
                <span className="ml-2 rounded-full bg-red-500 px-1.5 py-0.5 text-[10px] font-bold text-white">
                  {alerts.length}
                </span>
              )}
            </NavLink>
          ))}
          {user?.role === "ADMIN" && (
            <NavLink
              to="/admin"
              className={({ isActive }) =>
                `block rounded-lg px-3 py-2 text-sm font-medium ${
                  isActive ? "bg-brand-600 text-white" : "text-slate-300 hover:bg-slate-800 hover:text-white"
                }`
              }
            >
              Admin
            </NavLink>
          )}
        </nav>
        <div className="border-t border-slate-800 px-4 py-3">
          <div className="flex items-center gap-2 text-xs">
            <span className={`h-2 w-2 rounded-full ${connected ? "bg-emerald-400" : "bg-slate-600"}`} />
            <span className="text-slate-400">{connected ? "Live" : "Offline"}</span>
          </div>
          <p className="mt-1 text-xs text-slate-500">{user?.name}</p>
          <button
            onClick={() => {
              localStorage.clear();
              nav("/login");
            }}
            className="mt-2 w-full rounded-md bg-slate-800 px-2 py-1.5 text-xs text-slate-300 hover:bg-slate-700"
          >
            Sign out
          </button>
        </div>
      </aside>

      <div className="flex-1 flex flex-col min-w-0">
        {banner && (
          <div className="bg-red-600 text-white px-6 py-3 flex items-center justify-between animate-pulse">
            <div className="flex items-center gap-3">
              <span className="text-xl">🚨</span>
              <div>
                <p className="font-bold text-sm">HOTLIST VEHICLE DETECTED</p>
                <p className="text-xs text-red-100">
                  <span className="font-mono font-bold">{banner.plate}</span> · {banner.latitude.toFixed(5)},{" "}
                  {banner.longitude.toFixed(5)} · {new Date(banner.timestamp).toLocaleTimeString()} ·{" "}
                  {banner.confidence ? `${Math.round(banner.confidence * 100)}%` : "—"}
                </p>
              </div>
            </div>
            <Link
              to={`/vehicles/${banner.plate}`}
              className="rounded-lg bg-white px-3 py-1.5 text-xs font-bold text-red-700 hover:bg-red-50"
            >
              VIEW VEHICLE →
            </Link>
          </div>
        )}
        <main className="flex-1 overflow-auto p-6">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
