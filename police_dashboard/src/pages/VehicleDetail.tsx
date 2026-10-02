import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft, MapPin, RadioTower } from "lucide-react";
import { api } from "../services/api";
import { Button } from "../components/ui/Button";
import { Card, CardHeader } from "../components/ui/Card";
import { HotlistChip } from "../components/ui/HotlistChip";
import { StatusChip } from "../components/ui/StatusChip";
import { Skeleton } from "../components/ui/Feedback";
import type { MapPoint } from "../components/map/VehicleMap";
import { formatCoords, timeAgo } from "../lib/format";

// Leaflet is ~150 kB gzipped; it only loads once an officer opens this page,
// and separately from the page shell so the header appears immediately.
const VehicleMap = lazy(() => import("../components/map/VehicleMap"));

interface VehicleDetailResponse {
  plate: string;
  sightings_count: number;
  hotlist: {
    id: string;
    status: string;
    fir_reference?: string;
    expiry_at?: string;
    last_seen_at?: string;
    last_seen_lat?: number;
    last_seen_lng?: number;
  };
  complaint?: { type?: string; status?: string; description?: string } | null;
}

export default function VehicleDetail() {
  const { plate } = useParams();
  const [detail, setDetail] = useState<VehicleDetailResponse | null>(null);
  const [timeline, setTimeline] = useState<MapPoint[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    api
      .get<VehicleDetailResponse>(`/vehicles/${plate}`)
      .then((r) => setDetail(r.data))
      .catch((e) => {
        const d = e.response?.data?.detail;
        setError(typeof d === "string" ? d : "Failed to load this vehicle");
      })
      .finally(() => setLoading(false));
    api
      .get<MapPoint[]>(`/vehicles/${plate}/timeline`)
      .then((r) => setTimeline(r.data))
      .catch(() => {});
  }, [plate]);

  useEffect(load, [load]);

  async function patch(status: string) {
    if (!detail) return;
    setBusy(true);
    try {
      await api.patch(`/hotlist/${detail.hotlist.id}`, { status });
      load();
    } catch (e: any) {
      const d = e.response?.data?.detail;
      setError(typeof d === "string" ? d : "Could not update the hotlist entry");
    } finally {
      setBusy(false);
    }
  }

  if (error) {
    return (
      <Card padding="none" className="border-danger/40">
        <div role="alert" className="p-6 text-sm text-danger">
          <p className="font-semibold">{error}</p>
          <Link to="/search" className="mt-2 inline-block font-medium underline">
            Search for another vehicle
          </Link>
        </div>
      </Card>
    );
  }

  if (loading && !detail) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-8 w-64" />
        <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
          {[0, 1, 2, 3].map((i) => (
            <Skeleton key={i} className="h-24 rounded-xl" />
          ))}
        </div>
        <Skeleton className="h-[420px] rounded-xl" />
      </div>
    );
  }

  if (!detail) return null;

  const h = detail.hotlist;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <Link
            to="/hotlist"
            className="inline-flex items-center gap-1.5 text-sm font-medium text-primary-600 hover:text-primary-700"
          >
            <ArrowLeft className="h-4 w-4" aria-hidden /> Hotlist
          </Link>
          <div className="mt-3 flex flex-wrap items-center gap-3">
            {/* Plate badge keeps the on-vehicle look at every size. */}
            <span className="inline-flex items-center rounded-md border-2 border-slate-900 bg-white px-4 py-2 font-mono text-2xl font-bold tracking-[0.2em] text-slate-900 shadow-sm">
              {detail.plate}
            </span>
            <HotlistChip status={h.status} className="px-3 py-1.5 text-sm" />
            {h.fir_reference ? (
              <span className="font-mono text-xs text-surface-muted">FIR {h.fir_reference}</span>
            ) : null}
          </div>
        </div>

        {h.status === "ACTIVE" || h.status === "FIR_CONFIRMED" ? (
          <div className="flex flex-wrap gap-2">
            <Button size="sm" loading={busy} onClick={() => patch("FIR_CONFIRMED")}>
              Confirm FIR
            </Button>
            <Button size="sm" loading={busy} onClick={() => patch("RECOVERED")}>
              Mark recovered
            </Button>
            <Button size="sm" variant="secondary" loading={busy} onClick={() => patch("CLOSED")}>
              Close
            </Button>
          </div>
        ) : null}
      </div>

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        {[
          { label: "Total sightings", value: detail.sightings_count },
          { label: "Last detected", value: h.last_seen_at ? timeAgo(h.last_seen_at) : "—" },
          {
            label: "Last location",
            value:
              h.last_seen_lat != null ? formatCoords(h.last_seen_lat, h.last_seen_lng) : "—",
            mono: true,
          },
          { label: "FIR window ends", value: h.expiry_at ? timeAgo(h.expiry_at) : "—" },
        ].map((kpi) => (
          <Card key={kpi.label} padding="sm">
            <p className="text-xs font-medium text-surface-muted">{kpi.label}</p>
            <p
              className={[
                "mt-1.5 font-semibold text-surface-text",
                kpi.mono ? "font-mono text-sm" : "text-xl",
              ].join(" ")}
            >
              {kpi.value}
            </p>
          </Card>
        ))}
      </div>

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-5">
        <Card padding="none" className="overflow-hidden xl:col-span-3">
          <div className="p-5 pb-0">
            <CardHeader
              title="Tracking map"
              subtitle={`${timeline.length} sighting${timeline.length === 1 ? "" : "s"} · chronological route`}
            />
          </div>
          <div className="mt-4">
            <Suspense
              fallback={
                <div className="flex h-[420px] items-center justify-center bg-surface-bg text-sm text-surface-muted">
                  <Skeleton className="h-full w-full rounded-none" />
                </div>
              }
            >
              <VehicleMap plate={detail.plate} points={timeline} />
            </Suspense>
          </div>
        </Card>

        <div className="space-y-6 xl:col-span-2">
          <Card padding="none">
            <div className="p-5 pb-0">
              <CardHeader title="Detection timeline" subtitle="Newest first, numbered as on the map." />
            </div>
            {timeline.length === 0 ? (
              <p className="px-5 py-8 text-center text-sm text-surface-muted">
                No sightings recorded for this vehicle yet.
              </p>
            ) : (
              <ol className="scrollbar-thin mt-4 max-h-[420px] divide-y divide-surface-border overflow-y-auto">
                {[...timeline].reverse().map((t, i) => {
                  const n = timeline.length - i;
                  return (
                    <li key={t.id} className="flex items-start gap-3 px-5 py-3 transition-colors hover:bg-surface-bg">
                      <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-solid-danger text-[10px] font-bold text-white">
                        {n}
                      </span>
                      <div className="min-w-0">
                        <p className="text-xs font-semibold text-surface-text">
                          {new Date(t.detected_at).toLocaleString()}
                        </p>
                        <p className="mt-0.5 inline-flex items-center gap-1.5 font-mono text-xs text-surface-muted">
                          <MapPin className="h-3 w-3" aria-hidden />
                          {formatCoords(t.latitude, t.longitude)}
                        </p>
                        <p className="mt-0.5 inline-flex items-center gap-1.5 text-[11px] text-surface-subtle">
                          <RadioTower className="h-3 w-3" aria-hidden />
                          {t.confidence ? `${Math.round(t.confidence * 100)}% conf · ` : ""}
                          device {t.device_id.slice(0, 8)}…
                        </p>
                      </div>
                    </li>
                  );
                })}
              </ol>
            )}
          </Card>

          {detail.complaint ? (
            <Card>
              <CardHeader title="Originating complaint" />
              <dl className="mt-4 space-y-2 text-sm">
                {detail.complaint.type ? (
                  <div className="flex items-center justify-between gap-3">
                    <dt className="text-surface-muted">Type</dt>
                    <dd className="font-medium capitalize text-surface-text">
                      {detail.complaint.type.replace(/_/g, " ")}
                    </dd>
                  </div>
                ) : null}
                {detail.complaint.status ? (
                  <div className="flex items-center justify-between gap-3">
                    <dt className="text-surface-muted">Status</dt>
                    <dd>
                      <StatusChip status={detail.complaint.status} />
                    </dd>
                  </div>
                ) : null}
              </dl>
              {detail.complaint.description ? (
                <p className="mt-3 border-t border-surface-border pt-3 text-sm text-surface-muted">
                  {detail.complaint.description}
                </p>
              ) : null}
            </Card>
          ) : null}
        </div>
      </div>
    </div>
  );
}
