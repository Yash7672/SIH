import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, Complaint, STATUS_COLORS } from "../services/api";

export default function MyComplaints() {
  const [complaints, setComplaints] = useState<Complaint[]>([]);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState("ALL");

  useEffect(() => {
    api.get<Complaint[]>("/complaints/mine")
      .then((r) => setComplaints(r.data))
      .finally(() => setLoading(false));
  }, []);

  const filtered = filter === "ALL" ? complaints : complaints.filter((c) => c.status === filter);

  return (
    <div>
      <div className="mb-4 flex items-center justify-between">
        <h1 className="text-xl font-bold text-slate-900">My Complaints</h1>
        <select
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          className="rounded-lg border border-slate-300 px-3 py-1.5 text-sm"
        >
          {["ALL", "PENDING", "UNDER_REVIEW", "VERIFIED", "REJECTED", "HOTLISTED", "CLOSED"].map((s) => (
            <option key={s}>{s}</option>
          ))}
        </select>
      </div>

      <div className="rounded-xl bg-white ring-1 ring-slate-200">
        {loading ? (
          <div className="p-8 text-center text-sm text-slate-400">Loading…</div>
        ) : filtered.length === 0 ? (
          <div className="p-8 text-center text-sm text-slate-500">No complaints found.</div>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-slate-100 text-left text-xs uppercase tracking-wide text-slate-500">
                <th className="px-5 py-3">Plate</th>
                <th className="px-5 py-3">Type</th>
                <th className="px-5 py-3">Status</th>
                <th className="px-5 py-3">Submitted</th>
                <th className="px-5 py-3">Updated</th>
                <th className="px-5 py-3"></th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {filtered.map((c) => (
                <tr key={c.id} className="hover:bg-slate-50">
                  <td className="px-5 py-3 font-mono font-semibold">{c.plate}</td>
                  <td className="px-5 py-3 text-slate-600">{c.complaint_type}</td>
                  <td className="px-5 py-3">
                    <span className={`inline-flex rounded-full px-2.5 py-0.5 text-xs font-medium ring-1 ring-inset ${STATUS_COLORS[c.status]}`}>
                      {c.status.replace(/_/g, " ")}
                    </span>
                  </td>
                  <td className="px-5 py-3 text-slate-500">{new Date(c.created_at).toLocaleString()}</td>
                  <td className="px-5 py-3 text-slate-500">{new Date(c.updated_at).toLocaleString()}</td>
                  <td className="px-5 py-3">
                    <Link to={`/complaints/${c.id}`} className="text-brand-600 hover:underline">View</Link>
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
