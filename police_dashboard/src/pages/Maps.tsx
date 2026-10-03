import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { MapContainer, TileLayer } from "react-leaflet";
import { Layers, RefreshCw, TrafficCone } from "lucide-react";
import { api } from "../services/api";
import type { HeatLayerName, HeatResponse, HeatVehicleClass } from "../services/api";
import HeatLayer from "../components/map/HeatLayer";
import HeatFilters from "../components/map/HeatFilters";
import { CellTable, HeatLegend } from "../components/map/HeatChrome";
import { Card, CardHeader } from "../components/ui/Card";
import { Button } from "../components/ui/Button";
import { StatCard } from "../components/ui/StatCard";
import { EmptyState, ErrorState } from "../components/ui/Feedback";
import { PageHeader } from "../components/ui/Overlay";

/** Hyderabad, matching the fallback centre already used by VehicleDetail. */
const DEFAULT_CENTER: [number, number] = [17.385, 78.4867];

const TRAFFIC_GRADIENT: Record<number, string> = {
  0.2: "#2F4BD8",
  0.4: "#22C55E",
  0.6: "#F59E0B",
  0.8: "#F97316",
  1.0: "#DC2626",
};

const STOLEN_GRADIENT: Record<number, string> = {
  0.15: "#7C3AED",
  0.45: "#DC2626",
  1.0: "#FCA5A5",
};

function cx(...parts: Array<string | false | null | undefined>) {
  return parts.filter(Boolean).join(" ");
}

export default function Maps() {
  const [layer, setLayer] = useState<HeatLayerName>("traffic");
  const [minutes, setMinutes] = useState<number>(60);
  const [vehicleClass, setVehicleClass] = useState<HeatVehicleClass | "">("");
  const [data, setData] = useState<HeatResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [live, setLive] = useState(true);

  // Guards against a slow response for an old filter landing after a newer one.
  const requestSeq = useRef(0);

  const load = useCallback(async () => {
    const seq = ++requestSeq.current;
    setLoading(true);
    setError(null);
    try {
      const to = new Date();
      const from = new Date(to.getTime() - minutes * 60_000);
      const { data: body } = await api.get<HeatResponse>("/geo/heat", {
        params: {
          layer,
          from: from.toISOString(),
          to: to.toISOString(),
          ...(vehicleClass ? { vehicle_class: vehicleClass } : {}),
        },
      });
      if (seq !== requestSeq.current) return;
      setData(body);
    } catch (e) {
      if (seq !== requestSeq.current) return;
      const status = (e as { response?: { status?: number } })?.response?.status;
      setError(
        status === 403
          ? "Your role does not have access to the density map."
          : status === 422
            ? "The dashboard asked for a window the server rejected. Try a shorter one."
            : "The density map could not be loaded."
      );
      setData(null);
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  }, [layer, minutes, vehicleClass]);

  useEffect(() => {
    void load();
  }, [load]);

  // Poll while live. 15 s is slower than the server's 10 s cache, so a poll can
  // never be served a stale-but-fresh-looking response from a previous run.
  useEffect(() => {
    if (!live) return undefined;
    const id = setInterval(() => void load(), 15_000);
    return () => clearInterval(id);
  }, [live, load]);

  const peak = data?.cells.reduce((m, c) => Math.max(m, c.w), 0) ?? 0;
  const busy = data?.cells.filter((c) => c.w >= peak * 0.6).length ?? 0;

  const summary = useMemo(() => {
    if (!data || data.cells.length === 0) return null;
    const top = data.cells[0];
    if (layer === "stolen") {
      return `${data.cells.length} sighting cell${data.cells.length === 1 ? "" : "s"}, decaying over ${data.tau_hours ?? "?"} h.`;
    }
    return `Busiest cell: ${top.w.toFixed(1)} vehicles per frame across ${top.n} sampled hour${top.n === 1 ? "" : "s"}.`;
  }, [data, layer]);

  const legendLabel =
    layer === "traffic" ? "vehicles per frame" : "decayed sightings";

  const gradient = layer === "stolen" ? STOLEN_GRADIENT : TRAFFIC_GRADIENT;

  return (
    <div className="space-y-4">
      <PageHeader
        title="Maps"
        subtitle={
          <>
            Where vehicles were seen, aggregated into ~110 m cells. Counts only - no plate,
            image or device is attached to a cell.
          </>
        }
        action={
          <div className="flex items-center gap-2">
            <Button variant="secondary" size="sm" onClick={load} disabled={loading}>
              <RefreshCw className={cx("h-4 w-4", loading && "animate-spin")} aria-hidden />
              Refresh
            </Button>
            <Button
              variant={live ? "primary" : "secondary"}
              size="sm"
              onClick={() => setLive((v) => !v)}
              aria-pressed={live}
            >
              {live ? "Live" : "Paused"}
            </Button>
          </div>
        }
      />

      {error ? (
        <ErrorState title="Density map unavailable" description={error} onRetry={load} />
      ) : null}

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
        <StatCard
          label="Cells with data"
          value={data?.cells.length ?? "—"}
          icon={<Layers className="h-5 w-5" aria-hidden />}
          tone="info"
          hint={summary ? undefined : "no observations in this window"}
        />
        <StatCard
          label="Peak density"
          value={data && peak > 0 ? peak.toFixed(2) : "—"}
          icon={<TrafficCone className="h-5 w-5" aria-hidden />}
          tone="warning"
          hint={legendLabel}
        />
        <StatCard
          label="Hot cells"
          value={busy}
          icon={<Layers className="h-5 w-5" aria-hidden />}
          tone="danger"
          hint="at 60% of peak or above"
        />
      </div>

      <Card>
        <CardHeader
          title="Density"
          subtitle={
            data?.generated_at
              ? `Updated ${new Date(data.generated_at).toLocaleTimeString()}`
              : undefined
          }
        />

        <HeatFilters
          layer={layer}
          onLayer={setLayer}
          minutes={minutes}
          onMinutes={setMinutes}
          vehicleClass={vehicleClass}
          onVehicleClass={setVehicleClass}
        />


        {data && data.cells.length === 0 && !loading ? (
          <EmptyState
            title="No density in this window"
            description={
              layer === "traffic"
                ? "Traffic density is recorded only where a volunteer phone is actively scanning. Widen the window, or run scripts/seed_traffic.py to populate demo data."
                : "No hot-listed plate was sighted in this window."
            }
          />
        ) : (
          <div className="h-[560px] w-full">
            <MapContainer center={DEFAULT_CENTER} zoom={12} style={{ height: "100%", width: "100%" }}>
              <TileLayer
                attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
                url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
              />
              {data && data.cells.length > 0 ? (
                <HeatLayer
                  points={data.cells}
                  gradient={gradient}
                  // The stolen layer is sparse and exact; the traffic layer is
                  // a density field that benefits from a wider kernel.
                  radius={layer === "stolen" ? 18 : 30}
                  blur={layer === "stolen" ? 12 : 22}
                  minOpacity={0.3}
                  max={data.max}
                />
              ) : null}
            </MapContainer>
          </div>
        )}

        {data && data.cells.length > 0 ? (
          <HeatLegend gradient={gradient} legendLabel={legendLabel} summary={summary} />
        ) : null}
      </Card>

      <Card>
        <CardHeader title="Top cells" subtitle="Highest weight in the current window" />
        {data && data.cells.length > 0 ? (
          <CellTable cells={data.cells} layer={layer} />
        ) : (
          <EmptyState title="Nothing to rank" description="No cells in this window." />
        )}
      </Card>
    </div>
  );
}