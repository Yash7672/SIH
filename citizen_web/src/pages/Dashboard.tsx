import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, Complaint, STATUS_COLORS } from "../services/api";

function Badge({ status }: { status: string }) {
  return (
    <span className={`inline-flex rounded-full px-2.5 py-0.5 text-xs font-medium ring-1 ring-inset ${STATUS_COLORS[status] || "bg-slate-100 text-slate-700"}`}>
      {status.replace(/_/g, " ")}
    </span>
  );
}

export default function Dashboard() {
  const [complaints, setComplaints] = useState<Complaint[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api.get<Complaint[]>("/complaints/mine")
      .then((r) => setComplaints(r.data))
      .finally(() => setLoading(false));
  }, []);

  const pending = complaints.filter((c) => c.status === "PENDING" || c.status === "UNDER_REVIEW").length;
  const hotlisted = complaints.filter((c) => c.status === "HOTLISTED").length;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-bold text-slate-900">Dashboard</h1>
        <Link to="/complaints/new" className="rounded-lg bg-brand-600 px-4 py-2 text-sm font-semibold text-white hover:bg-brand-700">
          + File Complaint
        </Link>
      </div>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
        <div className="rounded-xl bg-white p-5 ring-1 ring-slate-200">
          <p className="text-sm text-slate-500">Total complaints</p>
          <p className="mt-1 text-3xl font-bold text-slate-900">{loading ? "—" : complaints.length}</p>
        </div>
        <div className="rounded-xl bg-white p-5 ring-1 ring-slate-200">
          <p className="text-sm text-slate-500">In review</p>
          <p className="mt-1 text-3xl font-bold text-amber-600">{loading ? "—" : pending}</p>
        </div>
        <div className="rounded-xl bg-white p-5 ring-1 ring-slate-200">
          <p className="text-sm text-slate-500">Hotlisted</p>
          <p className="mt-1 text-3xl font-bold text-red-600">{loading ? "—" : hotlisted}</p>
        </div>
      </div>

      <div className="rounded-xl bg-white ring-1 ring-slate-200">
        <div className="border-b border-slate-100 px-5 py-3 font-semibold text-slate-700">Recent complaints</div>
        {loading ? (
          <div className="p-8 text-center text-sm text-slate-400">Loading…</div>
        ) : complaints.length === 0 ? (
          <div className="p-8 text-center">
            <p className="text-sm text-slate-500">No complaints yet.</p>
            <Link to="/complaints/new" className="mt-2 inline-block text-sm font-medium text-brand-600 hover:underline">
              File your first complaint →
            </Link>
          </div>
        ) : (
          <ul className="divide-y divide-slate-100">
            {complaints.slice(0, 5).map((c) => (
              <li key={c.id} className="flex items-center justify-between px-5 py-3">
                <div>
                  <Link to={`/complaints/${c.id}`} className="font-mono font-semibold text-slate-900 hover:text-brand-600">
                    {c.plate}
                  </Link>
                  <p className="text-xs text-slate-500">{c.complaint_type} · {new Date(c.created_at).toLocaleString()}</p>
                </div>
                <Badge status={c.status} />
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
