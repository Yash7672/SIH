import { FormEvent, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Search } from "lucide-react";
import { Button } from "../components/ui/Button";
import { Card } from "../components/ui/Card";
import { PageHeader } from "../components/ui/Overlay";

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
      <PageHeader
        title="Vehicle search"
        subtitle="Enter a registration to open its tracking history, sightings and route."
      />

      <Card className="mt-6">
        <form onSubmit={submit} className="flex flex-col gap-3 sm:flex-row">
          <input
            value={plate}
            onChange={(e) => setPlate(e.target.value.toUpperCase())}
            placeholder="TS09AB1234"
            aria-label="Registration number"
            spellCheck={false}
            autoCapitalize="characters"
            className="h-12 flex-1 rounded-lg border border-surface-border bg-surface-card px-4 font-mono text-lg uppercase tracking-widest text-surface-text placeholder:font-sans placeholder:tracking-normal placeholder:text-surface-subtle focus:border-primary-600 focus:outline-none focus:ring-2 focus:ring-primary-600/25"
          />
          <Button type="submit" size="lg" icon={<Search className="h-4 w-4" aria-hidden />}>
            Search
          </Button>
        </form>
        <p className="mt-3 text-xs text-surface-muted">
          Spaces and dashes are removed automatically. Only the 10-character registration is accepted by the
          tracking API.
        </p>
      </Card>
    </div>
  );
}
