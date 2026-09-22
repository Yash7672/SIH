import { FormEvent, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, Complaint } from "../services/api";

export default function NewComplaint() {
  const nav = useNavigate();
  const [plate, setPlate] = useState("");
  const [type, setType] = useState("stolen");
  const [description, setDescription] = useState("");
  const [proof, setProof] = useState<File | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      const form = new FormData();
      form.append("plate", plate);
      form.append("complaint_type", type);
      form.append("description", description);
      if (proof) form.append("proof", proof);
      const { data } = await api.post<Complaint>("/complaints", form, {
        headers: { "Content-Type": "multipart/form-data" },
      });
      nav(`/complaints/${data.id}`);
    } catch (err: any) {
      const d = err.response?.data?.detail;
      setError(typeof d === "string" ? d : "Submission failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="mx-auto max-w-xl">
      <h1 className="mb-1 text-xl font-bold text-slate-900">File a complaint</h1>
      <p className="mb-6 text-sm text-slate-500">
        Report a stolen or missing vehicle. Police will verify before it enters the hotlist.
      </p>
      <form onSubmit={submit} className="space-y-4 rounded-xl bg-white p-6 ring-1 ring-slate-200">
        <div>
          <label className="mb-1 block text-sm font-medium text-slate-700">Vehicle Number</label>
          <input
            required
            value={plate}
            onChange={(e) => setPlate(e.target.value.toUpperCase())}
            placeholder="TS09AB1234"
            className="w-full rounded-lg border border-slate-300 px-3 py-2 font-mono text-sm uppercase tracking-wide focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-200"
          />
        </div>
        <div>
          <label className="mb-1 block text-sm font-medium text-slate-700">Complaint Type</label>
          <select
            value={type}
            onChange={(e) => setType(e.target.value)}
            className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-200"
          >
            <option value="stolen">Stolen vehicle</option>
            <option value="missing">Missing vehicle</option>
            <option value="hit_and_run">Hit and run</option>
            <option value="suspicious">Suspicious activity</option>
          </select>
        </div>
        <div>
          <label className="mb-1 block text-sm font-medium text-slate-700">Description</label>
          <textarea
            rows={4}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="When and where was the vehicle last seen?"
            className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-200"
          />
        </div>
        <div>
          <label className="mb-1 block text-sm font-medium text-slate-700">Proof (optional: image/PDF, max 5MB)</label>
          <input
            type="file"
            accept=".jpg,.jpeg,.png,.pdf"
            onChange={(e) => setProof(e.target.files?.[0] || null)}
            className="w-full text-sm text-slate-500 file:mr-3 file:rounded-lg file:border-0 file:bg-brand-50 file:px-4 file:py-2 file:text-sm file:font-medium file:text-brand-700 hover:file:bg-brand-100"
          />
        </div>
        {error && <div className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700 ring-1 ring-red-200">{error}</div>}
        <button
          type="submit"
          disabled={loading}
          className="w-full rounded-lg bg-brand-600 px-4 py-2.5 text-sm font-semibold text-white hover:bg-brand-700 disabled:opacity-50"
        >
          {loading ? "Submitting…" : "Submit complaint"}
        </button>
      </form>
    </div>
  );
}
