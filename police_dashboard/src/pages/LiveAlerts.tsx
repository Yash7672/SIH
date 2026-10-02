import { memo, useMemo } from "react";
import { Link } from "react-router-dom";
import { MapPin, RadioTower, Siren } from "lucide-react";
import { AlertEvent } from "../services/api";
import { SOCKET_LABELS } from "../components/Layout";
import { useSocketState } from "../hooks/SocketState";
import { PageHeader } from "../components/ui/Overlay";
import { formatCoords, platePath, timeAgo } from "../lib/format";

/** The console keeps at most this many alerts so a busy junction cannot
 *  grow the list without bound. App.tsx already caps the array. */
const DISPLAY_CAP = 100;

const AlertRow = memo(function AlertRow({ alert, isNew }: { alert: AlertEvent; isNew: boolean }) {
  const coords = useMemo(
    () => formatCoords(alert.latitude, alert.longitude),
    [alert.latitude, alert.longitude]
  );

  return (
    <article
      className={[
        "animate-slide-up rounded-xl border p-4 transition-colors sm:p-5",
        "border-danger/40 bg-danger/10 hover:bg-danger/15",
        isNew ? "ring-1 ring-danger/50" : "",
      ].join(" ")}
    >
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="flex min-w-0 items-start gap-3">
          <span
            className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-solid-danger text-white"
            aria-hidden
          >
            <Siren className="h-5 w-5" />
          </span>
          <div className="min-w-0">
            <p className="text-sm font-bold uppercase tracking-wide text-danger">
              Hotlist vehicle detected
            </p>
            <div className="mt-1.5 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-danger">
              <span className="inline-flex items-center gap-1.5">
                <MapPin className="h-3.5 w-3.5" aria-hidden />
                {coords}
              </span>
              <span className="inline-flex items-center gap-1.5">
                <RadioTower className="h-3.5 w-3.5" aria-hidden />
                {timeAgo(alert.timestamp)}
              </span>
              <span>{alert.confidence ? `${Math.round(alert.confidence * 100)}% confidence` : "Confidence n/a"}</span>
            </div>
          </div>
        </div>

        <Link
          to={platePath(alert.plate)}
          className="shrink-0 text-sm font-semibold text-danger underline-offset-2 hover:underline"
        >
          View on map →
        </Link>
      </div>

      <Link to={platePath(alert.plate)} className="mt-4 inline-block hover:opacity-85">
        {/* Real Indian plates are black-on-white with a black border; the badge
            reuses that look so the plate reads at a glance from across a desk. */}
        <span className="inline-flex items-center rounded-md border-2 border-slate-900 bg-white px-5 py-2 font-mono text-2xl font-bold tracking-[0.2em] text-slate-900 shadow-sm">
          {alert.plate}
        </span>
      </Link>
    </article>
  );
});

/**
 * Live / Reconnecting / Disconnected, with the last alert time.
 *
 * This page used to show nothing about the connection, so a dead stream was
 * indistinguishable from a quiet junction. The retry button is also the escape
 * hatch for a laptop that just changed network.
 */
function StreamStatusBanner() {
  const { status, detail, lastEventAt, reconnectNow } = useSocketState();

  const live = status === "live";
  const tone = live
    ? "border-success/40 bg-success/10 text-success"
    : status === "offline"
      ? "border-danger/40 bg-danger/10 text-danger"
      : "border-warning/40 bg-warning/10 text-warning";

  const lastEvent =
    lastEventAt === null
      ? "no alert received on this connection yet"
      : `last alert ${new Date(lastEventAt).toLocaleTimeString()}`;

  return (
    <div
      className={[
        "flex flex-wrap items-center justify-between gap-3 rounded-xl border px-4 py-3 text-sm",
        tone,
      ].join(" ")}
    >
      <span className="inline-flex items-center gap-2 font-medium">
        <span className="relative flex h-2 w-2" aria-hidden>
          {live ? (
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-current opacity-60" />
          ) : null}
          <span className="relative inline-flex h-2 w-2 rounded-full bg-current" />
        </span>
        Alert stream: {SOCKET_LABELS[status]}
        {detail ? ` - ${detail}` : ""}
      </span>

      <span className="flex items-center gap-3 text-xs opacity-80">
        <span>{lastEvent}</span>
        {live ? null : (
          <button
            type="button"
            onClick={reconnectNow}
            className="rounded-md border border-current px-2 py-1 font-semibold hover:bg-white/10"
          >
            Reconnect now
          </button>
        )}
      </span>
    </div>
  );
}

export default function LiveAlerts({ alerts }: { alerts: AlertEvent[] }) {
  const shown = alerts.slice(0, DISPLAY_CAP);

  return (
    <div className="space-y-6">
      <PageHeader
        title="Live Alerts"
        subtitle="Hotlist detections pushed over the WebSocket alert stream, newest first."
      />

      {/* Stream status. An operator has to be able to tell "nothing is happening"
          from "the feed died" - the old page looked identical in both cases. */}
      <StreamStatusBanner />

      <div className="flex flex-wrap items-center gap-3 rounded-xl border border-surface-border bg-surface-card px-4 py-3 text-sm">
        <span className="inline-flex items-center gap-2">
          <span className="relative flex h-2 w-2" aria-hidden>
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-danger opacity-60" />
            <span className="relative inline-flex h-2 w-2 rounded-full bg-danger" />
          </span>
          <span className="font-medium text-surface-text">
            {alerts.length} alert{alerts.length === 1 ? "" : "s"} in the last 24 hours
          </span>
        </span>
        {alerts.length > DISPLAY_CAP ? (
          <span className="text-xs text-surface-muted">Showing the newest {DISPLAY_CAP}.</span>
        ) : null}
      </div>

      {shown.length === 0 ? (
        <div className="rounded-2xl border border-dark-700 bg-dark-900 px-6 py-16 text-center">
          <RadioTower className="mx-auto h-8 w-8 text-accent-400" aria-hidden />
          <p className="mt-4 text-sm font-medium text-white">Waiting for hotlist detections…</p>
          <p className="mx-auto mt-1 max-w-sm text-xs text-dark-600">
            Alerts arrive automatically the moment a volunteer scanner reads a hot-listed plate. Keep this tab
            open and run the mobile scanner or the simulator to trigger one.
          </p>
        </div>
      ) : (
        <div className="space-y-3">
          {shown.map((a, i) => (
            <AlertRow key={a.sighting_id || `${a.plate}-${i}`} alert={a} isNew={i === 0} />
          ))}
        </div>
      )}
    </div>
  );
}
