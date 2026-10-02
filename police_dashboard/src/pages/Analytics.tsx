import { lazy, Suspense, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { BarChart3, MapPin, Trophy } from "lucide-react";
import { api } from "../services/api";
import { Card, CardHeader } from "../components/ui/Card";
import { PlateBadge } from "../components/ui/StatusChip";
import { EmptyState, Skeleton } from "../components/ui/Feedback";
import { PageHeader } from "../components/ui/Overlay";
import type { HourBucket } from "../components/charts/DetectionsChart";
import { formatCoords, platePath, timeAgo } from "../lib/format";

// Recharts is ~105 kB gzipped; it only loads once an officer opens Analytics.
const DetectionsChart = lazy(() => import("../components/charts/DetectionsChart"));

interface MatchRow {
  plate: string;
  sightings: number;
}

interface LocationRow {
  lat: number;
  lng: number;
  time: string;
}

export default function Analytics() {
  const [byHour, setByHour] = useState<HourBucket[]>([]);
  const [matches, setMatches] = useState<MatchRow[]>([]);
  const [locations, setLocations] = useState<LocationRow[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    // Each panel is independent: one slow or failing endpoint must not blank
    // the whole page.
    Promise.allSettled([
      api.get<{ hour: string; count: number }[]>("/analytics/detections"),
      api.get<MatchRow[]>("/analytics/hotlist-matches"),
      api.get<LocationRow[]>("/analytics/locations"),
    ])
      .then(([h, m, l]) => {
        if (cancelled) return;
        if (h.status === "fulfilled") {
          setByHour(
            h.value.data.map((r) => ({
              ...r,
              hour: new Date(r.hour).toLocaleString([], {
                month: "short",
                day: "numeric",
                hour: "2-digit",
                minute: "2-digit",
              }),
            }))
          );
        }
        if (m.status === "fulfilled") setMatches(m.value.data);
        if (l.status === "fulfilled") setLocations(l.value.data);
      })
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, []);

  if (loading) {
    return (
      <div className="space-y-6">
        <Skeleton className="h-8 w-40" />
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
          <Skeleton className="h-80 rounded-xl" />
          <Skeleton className="h-80 rounded-xl" />
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <PageHeader
        title="Analytics"
        subtitle="Detection throughput, the vehicles generating the most alerts, and the latest sighting pings."
      />

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <Card padding="none" className="overflow-hidden">
          <div className="p-5 pb-0">
            <CardHeader
              title="Detections by hour"
              subtitle="All ANPR sightings across registered devices."
            />
          </div>
          {byHour.length === 0 ? (
            <EmptyState
              icon={<BarChart3 className="h-6 w-6" aria-hidden />}
              title="No detection data yet"
              description="Run the mobile scanner or the simulator to generate sightings."
            />
          ) : (
            <div className="mt-4">
              <Suspense fallback={<Skeleton className="mx-4 mb-4 h-64 rounded-lg" />}>
                <DetectionsChart data={byHour} />
              </Suspense>
            </div>
          )}
        </Card>

        <Card padding="none">
          <div className="p-5 pb-0">
            <CardHeader title="Top hotlist vehicles" subtitle="Ranked by sighting count." />
          </div>
          {matches.length === 0 ? (
            <EmptyState
              icon={<Trophy className="h-6 w-6" aria-hidden />}
              title="No hotlist matches yet"
              description="Vehicles generate a ranking once their sightings have been recorded."
            />
          ) : (
            <ol className="mt-4 divide-y divide-surface-border">
              {matches.slice(0, 8).map((m, i) => (
                <li key={m.plate} className="flex items-center justify-between gap-3 px-5 py-3">
                  <div className="flex min-w-0 items-center gap-3">
                    <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-primary-50 text-[11px] font-bold text-primary-700">
                      {i + 1}
                    </span>
                    <Link to={platePath(m.plate)} className="hover:opacity-80">
                      <PlateBadge plate={m.plate} size="sm" />
                    </Link>
                  </div>
                  <span className="whitespace-nowrap text-xs text-surface-muted">
                    <b className="text-surface-text">{m.sightings}</b> sighting{m.sightings === 1 ? "" : "s"}
                  </span>
                </li>
              ))}
            </ol>
          )}
        </Card>
      </div>

      <Card padding="none">
        <div className="p-5 pb-0">
          <CardHeader
            title="Recent sighting locations"
            subtitle={`Latest ${locations.length} GPS pings from the detection network.`}
          />
        </div>
        {locations.length === 0 ? (
          <EmptyState
            icon={<MapPin className="h-6 w-6" aria-hidden />}
            title="No locations recorded"
            description="Locations appear once a registered scanner uploads a sighting."
          />
        ) : (
          <ul className="scrollbar-thin mt-4 max-h-80 divide-y divide-surface-border overflow-y-auto">
            {locations.map((l, i) => (
              <li key={`${l.time}-${i}`} className="flex items-center justify-between gap-3 px-5 py-2.5">
                <span className="inline-flex items-center gap-1.5 font-mono text-xs text-surface-text">
                  <MapPin className="h-3 w-3 text-surface-muted" aria-hidden />
                  {formatCoords(l.lat, l.lng)}
                </span>
                <span className="whitespace-nowrap text-xs text-surface-muted">{timeAgo(l.time)}</span>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
