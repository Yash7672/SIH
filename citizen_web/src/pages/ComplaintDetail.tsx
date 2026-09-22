import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, Complaint, STATUS_COLORS } from "../services/api";

const TIMELINE: Record<string, string[]> = {
  PENDING: ["Submitted"],
  UNDER_REVIEW: ["Submitted", "Under review"],
  VERIFIED: ["Submitted", "Under review", "Verified"],
  REJECTED: ["Submitted", "Under review", "Rejected"],
  HOTLISTED: ["Submitted", "Under review", "Verified", "Hotlisted"],
  CLOSED: ["Submitted", "Under review", "Verified", "Hotlisted", "Closed"],
};

export default function ComplaintDetail() {
  const { id } = useParams();
  const [complaint, setComplaint] = useState<Complaint | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    api.get<Complaint>(`/complaints/${id}`)
      .then((r) => setComplaint(r.data))
      .catch((e) => setError(e.response?.data?.detail || "Failed to load"));
  }, [id]);

  if (error) return <div className="rounded-lg bg-red-50 p-4 text-sm text-red-700">{error}</div>;
  if (!complaint) return <div className="p-8 text-center text-sm text-slate-400">Loading…</div>;

  const steps = TIMELINE[complaint.status] || ["Submitted"];
  const allSteps = ["Submitted", "Under review", "Verified", "Hotlisted"];
  const currentIndex = steps.length - 1;

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <div>
        <Link to="/complaints" className="text-sm text-brand-600 hover:underline">← Back to my complaints</Link>
        <div className="mt-2 flex items-center justify-between">
          <h1 className="font-mono text-2xl font-bold text-slate-900">{complaint.plate}</h1>
          <span className={`inline-flex rounded-full px-3 py-1 text-sm font-medium ring-1 ring-inset ${STATUS_COLORS[complaint.status]}`}>
            {complaint.status.replace(/_/g, " ")}
          </span>
        </div>
        <p className="mt-1 text-sm text-slate-500">
          Complaint ID: <code className="font-mono text-xs">{complaint.id}</code>
        </p>
      </div>

      <div className="rounded-xl bg-white p-6 ring-1 ring-slate-200">
        <h2 className="mb-4 font-semibold text-slate-700">Status timeline</h2>
        <ol className="flex items-center">
          {allSteps.map((step, i) => {
            const done = i <= currentIndex;
            return (
              <li key={step} className={`flex-1 ${i === 0 ? "" : ""}`}>
                <div className="flex flex-col items-center">
                  <div
                    className={`flex h-8 w-8 items-center justify-center rounded-full text-xs font-bold ${
                      done ? "bg-brand-600 text-white" : "bg-slate-200 text-slate-500"
                    }`}
                  >
                    {i + 1}
                  </div>
                  <span className={`mt-2 text-center text-xs ${done ? "text-slate-800 font-medium" : "text-slate-400"}`}>{step}</span>
                </div>
                {i < allSteps.length - 1 && (
                  <div className="hidden" />
                )}
              </li>
            );
          })}
        </ol>
      </div>

      <div className="rounded-xl bg-white p-6 ring-1 ring-slate-200">
        <h2 className="mb-3 font-semibold text-slate-700">Details</h2>
        <dl className="grid grid-cols-1 gap-3 text-sm sm:grid-cols-2">
          <div>
            <dt className="text-slate-500">Type</dt>
            <dd className="font-medium text-slate-900">{complaint.complaint_type}</dd>
          </div>
          <div>
            <dt className="text-slate-500">Submitted</dt>
            <dd className="font-medium text-slate-900">{new Date(complaint.created_at).toLocaleString()}</dd>
          </div>
          <div>
            <dt className="text-slate-500">Last updated</dt>
            <dd className="font-medium text-slate-900">{new Date(complaint.updated_at).toLocaleString()}</dd>
          </div>
          <div className="sm:col-span-2">
            <dt className="text-slate-500">Description</dt>
            <dd className="mt-1 text-slate-800">{complaint.description || "—"}</dd>
          </div>
        </dl>
      </div>

      {complaint.status === "HOTLISTED" && (
        <div className="rounded-xl bg-red-50 p-4 text-sm text-red-800 ring-1 ring-red-200">
          This vehicle has been added to the active hotlist. Volunteers' scanners will now flag sightings for police.
        </div>
      )}
    </div>
  );
}
