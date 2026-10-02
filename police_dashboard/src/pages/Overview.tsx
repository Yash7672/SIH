import { memo, useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  AlertTriangle,
  Car,
  CheckCircle2,
  RadioTower,
  ShieldAlert,
  Siren,
  SirenIcon,
} from "lucide-react";
import { api, AlertEvent, type HotlistEntry, type Overview as OverviewStats } from "../services/api";
import { Card, CardHeader } from "../components/ui/Card";
import { HotlistChip } from "../components/ui/HotlistChip";
import { PlateBadge } from "../components/ui/StatusChip";
import { StatCard } from "../components/ui/StatCard";
import { EmptyState, ErrorState, Skeleton } from "../components/ui/Feedback";
import { PageHeader } from "../components/ui/Overlay";
import { formatCoords, platePath, timeAgo } from "../lib/format";

/** One recent-alert row. Memoised because the list re-renders on every batch. */
const RecentAlertRow = memo(function RecentAlertRow({ alert }: { alert: AlertEvent }) {
  return (
    <li className="flex flex-wrap items-center justify-between gap-3 px-5 py-3 transition-colors hover:bg-surface-bg">
      <div className="min-w-0">
        <Link to={platePath(alert.plate)} className="inline-block hover:opacity-80">
          <PlateBadge plate={alert.plate} size="sm" />
        </Link>
        <p className="mt-1.5 text-xs text-surface-muted">
          {formatCoords(alert.latitude, alert.longitude)} · {timeAgo(alert.timestamp)}
          {alert.confidence ? ` · ${Math.round(alert.confidence * 100)}%` : ""}
        </p>
      </div>
      <span className="chip-danger inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium">
        <span className="h-1.5 w-1.5 rounded-full bg-danger" aria-hidden />
        Match
      </span>
    </li>
  );
});

const HotlistRow = memo(function HotlistRow({ entry }: { entry: HotlistEntry }) {
  return (
    <li className="flex flex-wrap items-center justify-between gap-3 px-5 py-3 transition-colors hover:bg-surface-bg">
      <Link to={platePath(entry.plate)} className="hover:opacity-80">
        <PlateBadge plate={entry.plate} size="sm" />
      </Link>
      <div className="flex items-center gap-3">
        <HotlistChip status={entry.status} />
        {entry.last_seen_at ? (
          <span className="text-xs text-surface-muted">seen {timeAgo(entry.last_seen_at)}</span>
        ) : null}
      </div>
    </li>
  );
});

export default function Overview({ alerts }: { alerts: AlertEvent[] }) {
  const [stats, setStats] = useState<OverviewStats | null>(null);
  const [recent, setRecent] = useState<AlertEvent[]>([]);
  const [active, setActive] = useState<HotlistEntry[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    // One request for each independent panel; Promise.allSettled keeps a
    // single failing panel from blanking the whole dashboard.
    Promise.allSettled([
      api.get<OverviewStats>("/analytics/overview"),
      api.get<AlertEvent[]>("/alerts"),
      api.get<HotlistEntry[]>("/hotlist?status_filter=ACTIVE"),
    ])
      .then(([s, r, h]) => {
        if (s.status === "fulfilled") setStats(s.value.data);
        if (r.status === "fulfilled") setRecent(r.value.data.slice(0, 8));
        if (h.status === "fulfilled") setActive(h.value.data.slice(0, 6));
        if (s.status === "rejected" && r.status === "rejected" && h.status === "rejected") {
          setError("Could not reach the API. Check that the backend is running.");
        }
      })
      .finally(() => setLoading(false));
  }, []);

  useEffect(load, [load]);

  // Live alerts take priority over the REST snapshot so the newest match is
  // always at the top of the list.
  const recentShown = alerts.length > 0 ? [...alerts, ...recent].slice(0, 8) : recent.slice(0, 8);

  return (
    <div className="space-y-6">
      <PageHeader
        title="Overview"
        subtitle="Live status of the hotlist, complaint queue and detection network."
        action={
          <Link
            to="/hotlist"
            className="text-sm font-medium text-primary-600 hover:text-primary-700 hover:underline"
          >
            View hotlist →
          </Link>
        }
      />

      {error ? <ErrorState title="Dashboard unavailable" description={error} onRetry={load} /> : null}

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-6">
        {loading && !stats ? (
          Array.from({ length: 6 }).map((_, i) => <Skeleton key={i} className="h-28 rounded-xl" />)
        ) : (
          <>
            <StatCard
              label="Active hotlist"
              value={stats?.active_hotlist ?? "—"}
              icon={<ShieldAlert className="h-5 w-5" aria-hidden />}
              tone="danger"
              hint="vehicles being watched"
            />
            <StatCard
              label="Pending complaints"
              value={stats?.pending_complaints ?? "—"}
              icon={<AlertTriangle className="h-5 w-5" aria-hidden />}
              tone="warning"
              hint="awaiting triage"
            />
            <StatCard
              label="Detections today"
              value={stats?.detections_today ?? "—"}
              icon={<RadioTower className="h-5 w-5" aria-hidden />}
              tone="info"
              hint="plates read by scanners"
            />
            <StatCard
              label="Hotlist matches"
              value={stats?.hotlist_matches_today ?? "—"}
              icon={<Siren className="h-5 w-5" aria-hidden />}
              tone="danger"
              hint="today"
            />
            <StatCard
              label="Live alerts"
              value={alerts.length}
              icon={<SirenIcon className="h-5 w-5" aria-hidden />}
              tone="danger"
              hint="this session"
            />
            <StatCard
              label="Devices online"
              value={stats?.active_devices ?? "—"}
              icon={<Car className="h-5 w-5" aria-hidden />}
              tone="success"
              hint="scanners reporting"
            />
          </>
        )}
      </div>

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-2">
        <Card padding="none">
          <div className="p-5 pb-0">
            <CardHeader
              title="Recent alerts"
              subtitle="Hotlist matches, newest first."
              action={
                <Link to="/alerts" className="text-sm font-medium text-primary-600 hover:text-primary-700">
                  All →
                </Link>
              }
            />
          </div>
          {loading ? (
            <div className="space-y-3 p-5">
              {[0, 1, 2].map((i) => (
                <Skeleton key={i} className="h-12" />
              ))}
            </div>
          ) : recentShown.length === 0 ? (
            <EmptyState
              icon={<Siren className="h-6 w-6" aria-hidden />}
              title="No alerts yet"
              description="When a volunteer scanner reads a hot-listed plate, it appears here within a second."
            />
          ) : (
            <ul className="mt-4 divide-y divide-surface-border">
              {recentShown.map((a, i) => (
                <RecentAlertRow key={a.sighting_id || i} alert={a} />
              ))}
            </ul>
          )}
        </Card>

        <Card padding="none">
          <div className="p-5 pb-0">
            <CardHeader
              title="Active hotlist"
              subtitle="Vehicles currently being watched."
              action={
                <Link to="/hotlist" className="text-sm font-medium text-primary-600 hover:text-primary-700">
                  All →
                </Link>
              }
            />
          </div>
          {loading ? (
            <div className="space-y-3 p-5">
              {[0, 1, 2].map((i) => (
                <Skeleton key={i} className="h-12" />
              ))}
            </div>
          ) : active.length === 0 ? (
            <EmptyState
              icon={<CheckCircle2 className="h-6 w-6" aria-hidden />}
              title="Hotlist is clear"
              description="No vehicles are on active watch. Verified complaints appear here automatically."
            />
          ) : (
            <ul className="mt-4 divide-y divide-surface-border">
              {active.map((h) => (
                <HotlistRow key={h.id} entry={h} />
              ))}
            </ul>
          )}
        </Card>
      </div>
    </div>
  );
}
