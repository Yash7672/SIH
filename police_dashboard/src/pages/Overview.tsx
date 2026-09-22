import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, AlertEvent, Overview, HOTLIST_STATUS_COLORS } from "../services/api";

function Kpi({ label, value, accent }: { label: string; value: string | number; accent?: string }) {
  return (
    <div className="rounded-xl bg-slate-900 p-5 ring-1 ring-slate-800">
      <p className="text-xs uppercase tracking-wide text-slate-500">{label}</p>
      <p className={`mt-1 text-3xl font-bold ${accent || "text-white"}`}>{value}</p>
    </div>
  );
}

export default function Overview({ alerts }: { alerts: AlertEvent[] }) {
  const [ov, setOv] = useState<Overview | null>(null);
  const [recent, setRecent] = useState<any[]>([]);

  useEffect(() => {
    api.get<Overview>("/analytics/overview").then((r) => setOv(r.data)).catch(() => {});
    api.get("/alerts").then((r) => setRecent(r.data)).catch(() => {});
  }, []);

  const recentShown = alerts.length > 0 ? [...alerts, ...recent].slice(0, 8) : recent.slice(0, 8);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-bold">Overview</h1>
        <Link to="/hotlist" className="text-sm text-brand-400 hover:underline">View hotlist →</Link>
      </div>

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-6">
        <Kpi label="Active hotlist" value={ov?.active_hotlist ?? "—"} accent="text-red-400" />
        <Kpi label="Pending complaints" value={ov?.pending_complaints ?? "—"} accent="text-amber-400" />
        <Kpi label="Detections today" value={ov?.detections_today ?? "—"} accent="text-blue-400" />
        <Kpi label="Hotlist matches" value={ov?.hotlist_matches_today ?? "—"} accent="text-red-400" />
        <Kpi label="Active alerts" value={alerts.length} accent="text-red-400" />
        <Kpi label="Recovered" value={ov?.recovered_vehicles ?? "—"} accent="text-emerald-400" />
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <div className="rounded-xl bg-slate-900 ring-1 ring-slate-800">
          <div className="border-b border-slate-800 px-5 py-3 flex items-center justify-between">
            <p className="font-semibold text-sm">Recent alerts</p>
            <Link to="/alerts" className="text-xs text-brand-400 hover:underline">All →</Link>
          </div>
          {recentShown.length === 0 ? (
            <div className="p-6 text-center text-sm text-slate-500">No recent alerts.</div>
          ) : (
            <ul className="divide-y divide-slate-800">
              {recentShown.map((a, i) => (
                <li key={a.sighting_id || i} className="px-5 py-3 flex items-center justify-between">
                  <div>
                    <Link to={`/vehicles/${a.plate}`} className="font-mono font-bold text-white hover:text-brand-400">
                      {a.plate}
                    </Link>
                    <p className="text-xs text-slate-500">
                      {Number(a.latitude).toFixed(5)}, {Number(a.longitude).toFixed(5)} ·{" "}
                      {new Date(a.timestamp).toLocaleTimeString()}
                    </p>
                  </div>
                  <span className="rounded-full bg-red-500/15 px-2 py-0.5 text-xs font-medium text-red-400 ring-1 ring-inset ring-red-500/30">
                    MATCH
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="rounded-xl bg-slate-900 ring-1 ring-slate-800">
          <div className="border-b border-slate-800 px-5 py-3 flex items-center justify-between">
            <p className="font-semibold text-sm">Active hotlist</p>
            <Link to="/hotlist" className="text-xs text-brand-400 hover:underline">All →</Link>
          </div>
          <HotlistMini />
        </div>
      </div>
    </div>
  );
}

function HotlistMini() {
  const [items, setItems] = useState<any[]>([]);
  useEffect(() => {
    api.get("/hotlist?status_filter=ACTIVE").then((r) => setItems(r.data.slice(0, 6))).catch(() => {});
  }, []);
  if (items.length === 0)
    return <div className="p-6 text-center text-sm text-slate-500">No active hotlist vehicles.</div>;
  return (
    <ul className="divide-y divide-slate-800">
      {items.map((h) => (
        <li key={h.id} className="px-5 py-3 flex items-center justify-between">
          <Link to={`/vehicles/${h.plate}`} className="font-mono font-bold text-white hover:text-brand-400">
            {h.plate}
          </Link>
          <div className="flex items-center gap-2">
            <span className={`rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${HOTLIST_STATUS_COLORS[h.status]}`}>
              {h.status}
            </span>
            {h.last_seen_at && (
              <span className="text-xs text-slate-500">seen {new Date(h.last_seen_at).toLocaleTimeString()}</span>
            )}
          </div>
        </li>
      ))}
    </ul>
  );
}
