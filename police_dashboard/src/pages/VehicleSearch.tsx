import { FormEvent, useState } from "react";
import { useNavigate } from "react-router-dom";

export default function VehicleSearch() {
  const [plate, setPlate] = useState("");
  const nav = useNavigate();

  function submit(e: FormEvent) {
    e.preventDefault();
    const p = plate.trim().toUpperCase();
    if (p) nav(`/vehicles/${encodeURIComponent(p)}`);
  }

  return (
    <div className="mx-auto max-w-xl">
      <h1 className="mb-2 text-xl font-bold">Vehicle Search</h1>
      <p className="mb-6 text-sm text-slate-500">Search a plate to view tracking history, sightings and route.</p>
      <form onSubmit={submit} className="flex gap-3">
        <input
          value={plate}
          onChange={(e) => setPlate(e.target.value.toUpperCase())}
          placeholder="TS09AB1234"
          className="flex-1 rounded-lg border border-slate-700 bg-slate-900 px-4 py-3 font-mono text-lg uppercase tracking-wide text-white placeholder-slate-600 focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-500/30"
        />
        <button
          type="submit"
          className="rounded-lg bg-brand-600 px-6 py-3 text-sm font-semibold text-white hover:bg-brand-700"
        >
          Search
        </button>
      </form>
    </div>
  );
}
