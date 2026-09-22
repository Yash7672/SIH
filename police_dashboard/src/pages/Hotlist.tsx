import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, HotlistEntry, HOTLIST_STATUS_COLORS } from "../services/api";

export default function Hotlist() {
  const [items, setItems] = useState<HotlistEntry[]>([]);
  const [filter, setFilter] = useState("ALL");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);

  function load() {
    setLoading(true);
    const q = filter === "ALL" ? "" : `?status_filter=${filter}`;
    api.get<HotlistEntry[]>(`/hotlist${q}`).then((r) => setItems(r.data)).finally(() => setLoading(false));
  }

  useEffect(load, [filter]);

  async function patch(id: string, status: string) {
    setBusy(id + status);
    try {
      await api.patch(`/hotlist/${id}`, { status });
      load();
    } finally {
      setBusy(null);
    }
  }

  return (
    <div>
      <div className="mb-4 flex items-center justify-between">
        <h1 className="text-xl font-bold">Hotlist</h1>
        <select
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          className="rounded-lg border border-slate-700 bg-slate-900 px-3 py-1.5 text-sm"
        >
          {["ALL", "ACTIVE", "FIR_CONFIRMED", "RECOVERED", "CLOSED", "EXPIRED"].map((s) => (
            <option key={s}>{s}</option>
          ))}
        </select>
      </div>

      <div className="rounded-xl bg-slate-900 ring-1 ring-slate-800 overflow-hidden">
        {loading ? (
          <div className="p-8 text-center text-sm text-slate-500">Loading…</div>
        ) : items.length === 0 ? (
          <div className="p-8 text-center text-sm text-slate-500">No hotlist entries.</div>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-slate-800 text-left text-xs uppercase tracking-wide text-slate-500">
                <th className="px-4 py-3">Plate</th>
                <th className="px-4 py-3">Status</th>
                <th className="px-4 py-3">Complaint</th>
                <th className="px-4 py-3">Added</th>
                <th className="px-4 py-3">FIR</th>
                <th className="px-4 py-3">Last seen</th>
                <th className="px-4 py-3">Location</th>
                <th className="px-4 py-3">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800/60">
              {items.map((h) => (
                <tr key={h.id} className="hover:bg-slate-800/40">
                  <td className="px-4 py-3">
                    <Link to={`/vehicles/${h.plate}`} className="font-mono font-semibold text-white hover:text-brand-400">
                      {h.plate}
                    </Link>
                  </td>
                  <td className="px-4 py-3">
                    <span className={`rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${HOTLIST_STATUS_COLORS[h.status]}`}>
                      {h.status.replace(/_/g, " ")}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-slate-500">{h.complaint_id ? h.complaint_id.slice(0, 8) : "—"}</td>
                  <td className="px-4 py-3 text-slate-500">{new Date(h.added_at).toLocaleString()}</td>
                  <td className="px-4 py-3 text-slate-500">{h.fir_reference || "—"}</td>
                  <td className="px-4 py-3 text-slate-500">
                    {h.last_seen_at ? new Date(h.last_seen_at).toLocaleString() : "—"}
                  </td>
                  <td className="px-4 py-3 text-slate-500">
                    {h.last_seen_lat != null ? `${h.last_seen_lat.toFixed(4)}, ${h.last_seen_lng?.toFixed(4)}` : "—"}
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex flex-wrap gap-1.5">
                      {h.status === "ACTIVE" && (
                        <>
                          <button
                            disabled={busy === h.id + "FIR_CONFIRMED"}
                            onClick={() => patch(h.id, "FIR_CONFIRMED")}
                            className="rounded bg-orange-600/80 px-2 py-0.5 text-[11px] font-semibold text-white hover:bg-orange-600 disabled:opacity-50"
                          >
                            Confirm FIR
                          </button>
                          <button
                            disabled={busy === h.id + "RECOVERED"}
                            onClick={() => patch(h.id, "RECOVERED")}
                            className="rounded bg-emerald-600/80 px-2 py-0.5 text-[11px] font-semibold text-white hover:bg-emerald-600 disabled:opacity-50"
                          >
                            Mark recovered
                          </button>
                          <button
                            disabled={busy === h.id + "CLOSED"}
                            onClick={() => patch(h.id, "CLOSED")}
                            className="rounded bg-slate-600 px-2 py-0.5 text-[11px] font-semibold text-white hover:bg-slate-500 disabled:opacity-50"
                          >
                            Close
                          </button>
                        </>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
