import { useEffect, useMemo } from "react";
import { MapContainer, TileLayer, Marker, Popup, Polyline, useMap } from "react-leaflet";
import L from "leaflet";

export interface MapPoint {
  id: string;
  latitude: number;
  longitude: number;
  detected_at: string;
  confidence?: number;
  device_id: string;
}

const alertIcon = L.divIcon({
  className: "",
  html: `<div style="background:#DC2626;color:#fff;border:2px solid #fff;border-radius:50%;width:28px;height:28px;display:flex;align-items:center;justify-content:center;font-weight:bold;font-size:12px;box-shadow:0 2px 6px rgba(0,0,0,.5);">!</div>`,
  iconSize: [28, 28],
  iconAnchor: [14, 14],
});

/** Fits the viewport to the sighting route once the points arrive. */
function FitBounds({ points }: { points: [number, number][] }) {
  const map = useMap();
  useEffect(() => {
    if (points.length > 0) map.fitBounds(L.latLngBounds(points), { padding: [40, 40] });
    // Re-fit only when the number of points changes, not on every array identity change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [points.length]);
  return null;
}

/** Default centre: Hyderabad, used only when there are no sightings yet. */
const FALLBACK_CENTER: [number, number] = [17.385, 78.4867];

export default function VehicleMap({ plate, points }: { plate: string; points: MapPoint[] }) {
  const latLngs = useMemo<[number, number][]>(
    () => points.map((p) => [p.latitude, p.longitude]),
    [points]
  );
  const center = latLngs.length > 0 ? latLngs[latLngs.length - 1] : FALLBACK_CENTER;

  return (
    <div className="h-[420px] w-full">
      <MapContainer center={center} zoom={12} style={{ height: "100%", width: "100%" }}>
        <TileLayer
          attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
          url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
        />
        <FitBounds points={latLngs} />
        {latLngs.length > 1 && (
          // Chronological route, oldest first.
          <Polyline positions={latLngs} pathOptions={{ color: "#2F4BD8", weight: 4, opacity: 0.85 }} />
        )}
        {points.map((p, i) => (
          <Marker key={p.id} position={[p.latitude, p.longitude]} icon={alertIcon}>
            <Popup>
              <div style={{ fontFamily: "monospace", fontSize: 12 }}>
                <strong>{plate}</strong>
                <br />
                Stop {i + 1} / {points.length}
                <br />
                Time: {new Date(p.detected_at).toLocaleTimeString()}
                <br />
                Lat: {p.latitude.toFixed(6)}
                <br />
                Lng: {p.longitude.toFixed(6)}
                <br />
                Confidence: {p.confidence ? `${Math.round(p.confidence * 100)}%` : "—"}
                <br />
                Device: {p.device_id.slice(0, 8)}…
              </div>
            </Popup>
          </Marker>
        ))}
      </MapContainer>
    </div>
  );
}
