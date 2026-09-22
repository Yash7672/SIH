import { Link } from "react-router-dom";
import { AlertEvent } from "../services/api";

export default function LiveAlerts({ alerts }: { alerts: AlertEvent[] }) {
  return (
    <div>
      <h1 className="mb-4 text-xl font-bold">Live Alerts</h1>
      <p className="mb-4 text-sm text-slate-500">
        Real-time hotlist detections pushed over WebSocket. {alerts.length} in this session.
      </p>
      {alerts.length === 0 ? (
        <div className="rounded-xl bg-slate-900 p-10 text-center ring-1 ring-slate-800">
          <p className="text-4xl mb-3">📡</p>
          <p className="text-sm text-slate-400">Waiting for hotlist detections…</p>
          <p className="mt-1 text-xs text-slate-600">Run the simulator or mobile scanner to trigger alerts.</p>
        </div>
      ) : (
        <div className="space-y-3">
          {alerts.map((a, i) => (
            <div
              key={a.sighting_id || i}
              className="rounded-xl bg-red-600/10 ring-1 ring-red-500/40 p-4 flex items-center justify-between"
            >
              <div className="flex items-center gap-4">
                <span className="text-2xl">🚨</span>
                <div>
                  <p className="font-bold text-red-400 text-sm">HOTLIST VEHICLE DETECTED</p>
                  <p className="text-sm">
                    <span className="font-mono font-bold text-white">{a.plate}</span>
                    <span className="text-slate-400">
                      {" "}· {a.latitude.toFixed(5)}, {a.longitude.toFixed(5)} ·{" "}
                      {new Date(a.timestamp).toLocaleTimeString()} ·{" "}
                      {a.confidence ? `${Math.round(a.confidence * 100)}%` : "—"}
                    </span>
                  </p>
                </div>
              </div>
              <Link
                to={`/vehicles/${a.plate}`}
                className="rounded-lg bg-red-600 px-3 py-1.5 text-xs font-bold text-white hover:bg-red-500"
              >
                VIEW VEHICLE
              </Link>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
