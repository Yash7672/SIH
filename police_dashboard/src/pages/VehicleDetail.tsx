import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { MapContainer, TileLayer, Marker, Popup, Polyline, useMap } from "react-leaflet";
import L from "leaflet";
import { api, HOTLIST_STATUS_COLORS } from "../services/api";

const alertIcon = L.divIcon({
  className: "",
  html: `<div style="background:#dc2626;color:#fff;border:2px solid #fff;border-radius:50%;width:28px;height:28px;display:flex;align-items:center;justify-content:center;font-weight:bold;font-size:12px;box-shadow:0 2px 6px rgba(0,0,0,.5);">R</div>`,
  iconSize: [28, 28],
  iconAnchor: [14, 14],
});

function FitBounds({ points }: { points: [number, number][] }) {
  const map = useMap();
  useEffect(() => {
    if (points.length > 0) {
      map.fitBounds(L.latLngBounds(points), { padding: [40, 40] });
    }
  }, [points.length]);
  return null;
}

interface TimelinePoint {
  id: string;
  latitude: number;
  longitude: number;
  detected_at: string;
  confidence?: number;
  device_id: string;
}

export default function VehicleDetail() {
  const { plate } = useParams();
  const [detail, setDetail] = useState<any>(null);
  const [timeline, setTimeline] = useState<TimelinePoint[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  function load() {
    api
      .get(`/vehicles/${plate}`)
      .then((r) => setDetail(r.data))
      .catch((e) => setError(e.response?.data?.detail || "Failed to load"));
    api
      .get<TimelinePoint[]>(`/vehicles/${plate}/timeline`)
      .then((r) => setTimeline(r.data))
      .catch(() => {});
  }

  useEffect(load, [plate]);

  async function patch(status: string) {
    if (!detail) return;
    setBusy(true);
    try {
      await api.patch(`/hotlist/${detail.hotlist.id}`, { status });
      load();
    } finally {
      setBusy(false);
    }
  }

  if (error)
    return (
      <div className="rounded-xl bg-red-500/10 p-6 text-sm text-red-400 ring-1 ring-red-500/30">
        {error}{" "}
        <Link to="/search" className="underline">
          Search again
        </Link>
      </div>
    );
  if (!detail) return <div className="p-8 text-center text-sm text-slate-500">Loading…</div>;

  const h = detail.hotlist;
  const points: [number, number][] = timeline.map((t) => [t.latitude, t.longitude]);
  const center: [number, number] =
    points.length > 0 ? points[points.length - 1] : [17.385, 78.4867];

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <Link to="/hotlist" className="text-xs text-brand-400 hover:underline">
            ← Hotlist
          </Link>
          <div className="mt-1 flex items-center gap-3">
            <h1 className="font-mono text-3xl font-bold text-white">{detail.plate}</h1>
            <span
              className={`rounded-full px-3 py-1 text-xs font-bold ring-1 ring-inset ${HOTLIST_STATUS_COLORS[h.status]}`}
            >
              {h.status.replace(/_/g, " ")}
            </span>
          </div>
        </div>
        <div className="flex gap-2">
          {h.status === "ACTIVE" && (
            <>
              <button
                disabled={busy}
                onClick={() => patch("FIR_CONFIRMED")}
                className="rounded-lg bg-orange-600 px-3 py-2 text-xs font-bold text-white hover:bg-orange-500 disabled:opacity-50"
              >
                Confirm FIR
              </button>
              <button
                disabled={busy}
                onClick={() => patch("RECOVERED")}
                className="rounded-lg bg-emerald-600 px-3 py-2 text-xs font-bold text-white hover:bg-emerald-500 disabled:opacity-50"
              >
                Mark Recovered
              </button>
              <button
                disabled={busy}
                onClick={() => patch("CLOSED")}
                className="rounded-lg bg-slate-700 px-3 py-2 text-xs font-bold text-slate-300 hover:bg-slate-600 disabled:opacity-50"
              >
                Close
              </button>
            </>
          )}
        </div>
      </div>

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <div className="rounded-xl bg-slate-900 p-4 ring-1 ring-slate-800">
          <p className="text-xs text-slate-500">Total sightings</p>
          <p className="mt-1 text-2xl font-bold text-white">{detail.sightings_count}</p>
        </div>
        <div className="rounded-xl bg-slate-900 p-4 ring-1 ring-slate-800">
          <p className="text-xs text-slate-500">Last detected</p>
          <p className="mt-1 text-sm font-semibold text-white">
            {h.last_seen_at ? new Date(h.last_seen_at).toLocaleString() : "—"}
          </p>
        </div>
        <div className="rounded-xl bg-slate-900 p-4 ring-1 ring-slate-800">
          <p className="text-xs text-slate-500">Last location</p>
          <p className="mt-1 text-sm font-semibold font-mono text-white">
            {h.last_seen_lat != null
              ? `${h.last_seen_lat.toFixed(5)}, ${h.last_seen_lng.toFixed(5)}`
              : "—"}
          </p>
        </div>
        <div className="rounded-xl bg-slate-900 p-4 ring-1 ring-slate-800">
          <p className="text-xs text-slate-500">Expiry (FIR window)</p>
          <p className="mt-1 text-sm font-semibold text-white">
            {h.expiry_at ? new Date(h.expiry_at).toLocaleString() : "—"}
          </p>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        <div className="lg:col-span-2 rounded-xl bg-slate-900 ring-1 ring-slate-800 overflow-hidden">
          <div className="border-b border-slate-800 px-5 py-3 flex items-center justify-between">
            <p className="font-semibold text-sm">Tracking map</p>
            <p className="text-xs text-slate-500">{timeline.length} sighting(s) · chronological route</p>
          </div>
          <div className="h-[420px]">
            <MapContainer center={center} zoom={12} style={{ height: "100%", width: "100%" }}>
              <TileLayer
                attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
                url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
              />
              <FitBounds points={points} />
              {points.length > 1 && (
                <Polyline positions={points} pathOptions={{ color: "#ef4444", weight: 4, opacity: 0.85 }} />
              )}
              {timeline.map((t, i) => (
                <Marker key={t.id} position={[t.latitude, t.longitude]} icon={alertIcon}>
                  <Popup>
                    <div style={{ fontFamily: "monospace", fontSize: 12 }}>
                      <strong>{detail.plate}</strong>
                      <br />
                      Marker {i + 1} / {timeline.length}
                      <br />
                      Time: {new Date(t.detected_at).toLocaleTimeString()}
                      <br />
                      Lat: {t.latitude.toFixed(6)}
                      <br />
                      Lng: {t.longitude.toFixed(6)}
                      <br />
                      Confidence: {t.confidence ? `${Math.round(t.confidence * 100)}%` : "—"}
                      <br />
                      Device: {t.device_id.slice(0, 8)}…
                    </div>
                  </Popup>
                </Marker>
              ))}
            </MapContainer>
          </div>
        </div>

        <div className="space-y-6">
          <div className="rounded-xl bg-slate-900 ring-1 ring-slate-800">
            <div className="border-b border-slate-800 px-5 py-3 font-semibold text-sm">Detection timeline</div>
            {timeline.length === 0 ? (
              <div className="p-6 text-center text-sm text-slate-500">No sightings yet.</div>
            ) : (
              <ol className="divide-y divide-slate-800/60 max-h-96 overflow-auto">
                {timeline.map((t, i) => (
                  <li key={t.id} className="px-5 py-3 flex items-start gap-3">
                    <span className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-red-600 text-[10px] font-bold text-white">
                      {i + 1}
                    </span>
                    <div className="min-w-0">
                      <p className="text-xs font-semibold text-white">
                        {new Date(t.detected_at).toLocaleString()}
                      </p>
                      <p className="text-xs font-mono text-slate-400 truncate">
                        {t.latitude.toFixed(5)}, {t.longitude.toFixed(5)}
                      </p>
                      <p className="text-[11px] text-slate-600">
                        {t.confidence ? `${Math.round(t.confidence * 100)}% conf · ` : ""}
                        device {t.device_id.slice(0, 8)}…
                      </p>
                    </div>
                  </li>
                ))}
              </ol>
            )}
          </div>

          {detail.complaint && (
            <div className="rounded-xl bg-slate-900 ring-1 ring-slate-800 p-5">
              <p className="mb-2 font-semibold text-sm">Complaint</p>
              <p className="text-xs text-slate-400">
                Type: <span className="text-white">{detail.complaint.type}</span>
              </p>
              <p className="text-xs text-slate-400">
                Status: <span className="text-white">{detail.complaint.status}</span>
              </p>
              <p className="mt-2 text-xs text-slate-500">{detail.complaint.description || "No description"}</p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
