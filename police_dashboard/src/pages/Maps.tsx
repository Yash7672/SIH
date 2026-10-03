import { useEffect, useState } from "react";
import { MapContainer, Marker, Popup, TileLayer, Circle, Polyline } from "react-leaflet";
import { useSearchParams } from "react-router-dom";
import { api } from "../services/api";
import { Button } from "../components/ui/Button";
import { Card } from "../components/ui/Card";
import { Skeleton } from "../components/ui/Feedback";

interface CameraMarker {
  id: string;
  name: string;
  lat: number;
  lng: number;
  online: boolean;
  sightings_today: number;
}

interface TrafficCell {
  lat: number;
  lng: number;
  count: number;
}

interface ODFlow {
  count: number;
  avg_travel_seconds: number;
  origin: { id: string; name: string; lat: number; lng: number };
  dest: { id: string; name: string; lat: number; lng: number };
}

const HYDERABAD_CENTER: [number, number] = [17.385, 78.4867];

export default function MapsPage() {
  const [searchParams] = useSearchParams();
  const targetPlate = searchParams.get("plate")?.trim() || "";
  const [cameras, setCameras] = useState<CameraMarker[]>([]);
  const [cells, setCells] = useState<TrafficCell[]>([]);
  const [flows, setFlows] = useState<ODFlow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError(null);

    Promise.all([
      api.get<{ cameras: CameraMarker[] }>('/maps/cameras').catch(() => ({ data: { cameras: [] } })),
      api.get<{ cells: TrafficCell[] }>('/maps/traffic?hours=6').catch(() => ({ data: { cells: [] } })),
      api.get<{ flows: ODFlow[] }>('/maps/od-flows?hours=6').catch(() => ({ data: { flows: [] } })),
    ])
      .then(([cameraRes, trafficRes, flowRes]) => {
        if (!alive) return;
        setCameras(cameraRes.data.cameras ?? []);
        setCells(trafficRes.data.cells ?? []);
        setFlows(flowRes.data.flows ?? []);
      })
      .catch(() => {
        if (!alive) return;
        setError("Unable to load live map data right now.");
      })
      .finally(() => {
        if (alive) setLoading(false);
      });

    return () => {
      alive = false;
    };
  }, []);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold text-surface-text">Maps</h1>
          <p className="text-sm text-surface-muted">
            Live camera status, traffic density, and OD corridors.
          </p>
        </div>
        {targetPlate ? (
          <Button size="sm" variant="secondary" onClick={() => window.history.back()}>
            Back to {targetPlate}
          </Button>
        ) : null}
      </div>

      {error ? (
        <Card className="border-danger/40 bg-danger/5 p-4 text-sm text-danger">{error}</Card>
      ) : null}

      {loading ? (
        <Skeleton className="h-[560px] w-full rounded-xl" />
      ) : (
        <div className="overflow-hidden rounded-xl border border-surface-border bg-surface-card">
          <div className="h-[560px] w-full">
            <MapContainer center={HYDERABAD_CENTER} zoom={11} style={{ height: "100%", width: "100%" }}>
              <TileLayer
                attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
                url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
              />

              {cells.map((cell, index) => (
                <Circle
                  key={`${cell.lat}-${cell.lng}-${index}`}
                  center={[cell.lat, cell.lng]}
                  radius={Math.max(250, cell.count * 150)}
                  pathOptions={{
                    color: cell.count > 10 ? "#ef4444" : cell.count > 5 ? "#f59e0b" : "#38bdf8",
                    fillColor: cell.count > 10 ? "#ef4444" : cell.count > 5 ? "#f59e0b" : "#38bdf8",
                    fillOpacity: 0.25,
                    weight: 1,
                  }}
                />
              ))}

              {flows.map((flow) => {
                const points: [number, number][] = [
                  [flow.origin.lat, flow.origin.lng],
                  [flow.dest.lat, flow.dest.lng],
                ];
                return (
                  <Polyline
                    key={`${flow.origin.id}-${flow.dest.id}`}
                    positions={points}
                    pathOptions={{ color: "#8b5cf6", weight: Math.max(2, Math.min(flow.count, 8)) }}
                  />
                );
              })}

              {cameras.map((camera) => (
                <Marker key={camera.id} position={[camera.lat, camera.lng]}>
                  <Popup>
                    <div className="space-y-1 text-xs text-surface-text">
                      <strong>{camera.name}</strong>
                      <div>{camera.online ? "Online" : "Offline"}</div>
                      <div>{camera.sightings_today} detections today</div>
                    </div>
                  </Popup>
                </Marker>
              ))}
            </MapContainer>
          </div>
        </div>
      )}

      <div className="grid gap-4 md:grid-cols-3">
        <Card padding="sm">
          <p className="text-xs font-medium uppercase tracking-[0.12em] text-surface-muted">Cameras</p>
          <p className="mt-2 text-2xl font-semibold text-surface-text">{cameras.length}</p>
        </Card>
        <Card padding="sm">
          <p className="text-xs font-medium uppercase tracking-[0.12em] text-surface-muted">Traffic cells</p>
          <p className="mt-2 text-2xl font-semibold text-surface-text">{cells.length}</p>
        </Card>
        <Card padding="sm">
          <p className="text-xs font-medium uppercase tracking-[0.12em] text-surface-muted">OD corridors</p>
          <p className="mt-2 text-2xl font-semibold text-surface-text">{flows.length}</p>
        </Card>
      </div>
    </div>
  );
}
