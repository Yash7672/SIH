import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, Complaint, COMPLAINT_STATUS_COLORS } from "../services/api";

export default function Complaints() {
  const [items, setItems] = useState<Complaint[]>([]);
  const [filter, setFilter] = useState("ALL");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);

  function load() {
    setLoading(true);
    const q = filter === "ALL" ? "" : `?status_filter=${filter}`;
    api.get<Complaint[]>(`/complaints${q}`).then((r) => setItems(r.data)).finally(() => setLoading(false));
  }

  useEffect(load, [filter]);

  async function action(id: string, kind: "verify" | "reject") {
    setBusy(id + kind);
    try {
      await api.post(`/complaints/${id}/${kind}`, kind === "verify" ? {} : {});
      load();
    } finally {
      setBusy(null);
    }
  }

  return (
    <div>
      <div className="mb-4 flex items-center justify-between">
        <h1 className="text-xl font-bold">Complaints</h1>
        <select
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          className="rounded-lg border border-slate-700 bg-slate-900 px-3 py-1.5 text-sm"
        >
          {["ALL", "PENDING", "UNDER_REVIEW", "VERIFIED", "REJECTED", "HOTLISTED", "CLOSED"].map((s) => (
            <option key={s}>{s}</option>
          ))}
        </select>
      </div>

      <div className="rounded-xl bg-slate-900 ring-1 ring-slate-800 overflow-hidden">
        {loading ? (
          <div className="p-8 text-center text-sm text-slate-500">Loading…</div>
        ) : items.length === 0 ? (
          <div className="p-8 text-center text-sm text-slate-500">No complaints found.</div>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-slate-800 text-left text-xs uppercase tracking-wide text-slate-500">
                <th className="px-4 py-3">Plate</th>
                <th className="px-4 py-3">Type</th>
                <th className="px-4 py-3">Status</th>
                <th className="px-4 py-3">Submitted</th>
                <th className="px-4 py-3">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800/60">
              {items.map((c) => (
                <tr key={c.id} className="hover:bg-slate-800/40">
                  <td className="px-4 py-3 font-mono font-semibold text-white">{c.plate}</td>
                  <td className="px-4 py-3 text-slate-400">{c.complaint_type}</td>
                  <td className="px-4 py-3">
                    <span className={`rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${COMPLAINT_STATUS_COLORS[c.status]}`}>
                      {c.status.replace(/_/g, " ")}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-slate-500">{new Date(c.created_at).toLocaleString()}</td>
                  <td className="px-4 py-3">
                    {(c.status === "PENDING" || c.status === "UNDER_REVIEW") && (
                      <div className="flex gap-2">
                        <button
                          disabled={busy === c.id + "verify"}
                          onClick={() => action(c.id, "verify")}
                          className="rounded-md bg-emerald-600 px-2.5 py-1 text-xs font-semibold text-white hover:bg-emerald-700 disabled:opacity-50"
                        >
                          Verify → Hotlist
                        </button>
                        <button
                          disabled={busy === c.id + "reject"}
                          onClick={() => action(c.id, "reject")}
                          className="rounded-md bg-slate-700 px-2.5 py-1 text-xs font-semibold text-slate-300 hover:bg-slate-600 disabled:opacity-50"
                        >
                          Reject
                        </button>
                      </div>
                    )}
                    {c.status === "HOTLISTED" && (
                      <Link to={`/vehicles/${c.plate}`} className="text-xs text-brand-400 hover:underline">
                        View vehicle →
                      </Link>
                    )}
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
