import type { ReactNode } from "react";
import { AlertCircle, Inbox, RefreshCw, ShieldAlert } from "lucide-react";
import { Button } from "./Button";

export function EmptyState({
  title,
  description,
  icon,
  action,
  className = "",
}: {
  title: string;
  description?: string;
  icon?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div className={["flex flex-col items-center justify-center px-6 py-14 text-center", className].join(" ")}>
      <div className="rounded-full bg-surface-raised p-3 text-surface-muted" aria-hidden>
        {icon ?? <Inbox className="h-6 w-6" />}
      </div>
      <p className="mt-4 text-sm font-semibold text-surface-text">{title}</p>
      {description ? (
        <p className="mt-1 max-w-sm text-sm text-surface-muted">{description}</p>
      ) : null}
      {action ? <div className="mt-4">{action}</div> : null}
    </div>
  );
}

export function ErrorState({
  title = "Something went wrong",
  description,
  onRetry,
  className = "",
}: {
  title?: string;
  description?: string | null;
  onRetry?: () => void;
  className?: string;
}) {
  return (
    <div
      className={[
        "flex flex-col items-center justify-center px-6 py-12 text-center",
        className,
      ].join(" ")}
      role="alert"
    >
      <div className="rounded-full bg-danger/10 p-3 text-danger" aria-hidden>
        <AlertCircle className="h-6 w-6" />
      </div>
      <p className="mt-4 text-sm font-semibold text-surface-text">{title}</p>
      {description ? <p className="mt-1 max-w-sm text-sm text-surface-muted">{description}</p> : null}
      {onRetry ? (
        <Button variant="secondary" size="sm" className="mt-4" icon={<RefreshCw className="h-3.5 w-3.5" />} onClick={onRetry}>
          Try again
        </Button>
      ) : null}
    </div>
  );
}

export function Skeleton({ className = "" }: { className?: string }) {
  return (
    <div
      className={["relative overflow-hidden rounded-md bg-surface-raised", className].join(" ")}
      aria-hidden
    >
      <div className="absolute inset-0 -translate-x-full animate-shimmer bg-gradient-to-r from-transparent via-surface-text/10 to-transparent" />
    </div>
  );
}

export function PageSkeleton({ rows = 3 }: { rows?: number }) {
  return (
    <div className="space-y-6" aria-busy="true" aria-label="Loading">
      <div className="space-y-2">
        <Skeleton className="h-7 w-52" />
        <Skeleton className="h-4 w-72" />
      </div>
      <div className="grid gap-4 sm:grid-cols-3">
        {[0, 1, 2].map((i) => (
          <Skeleton key={i} className="h-28" />
        ))}
      </div>
      <div className="space-y-3">
        {Array.from({ length: rows }).map((_, i) => (
          <Skeleton key={i} className="h-16" />
        ))}
      </div>
    </div>
  );
}

export function SkeletonCard({ className = "" }: { className?: string }) {
  return <Skeleton className={["h-32 rounded-xl", className].join(" ")} />;
}

/** Prominent banner for hotlist matches - the one thing an officer must not miss. */
export function HotlistBanner({
  plate,
  children,
  action,
}: {
  plate: string;
  children?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-4 rounded-xl border border-danger/40 bg-danger/10 p-4">
      <div className="flex min-w-0 items-center gap-3">
        <span className="shrink-0 rounded-lg bg-solid-danger p-2 text-white" aria-hidden>
          <ShieldAlert className="h-5 w-5" />
        </span>
        <div className="min-w-0">
          <p className="text-sm font-bold text-danger">Hotlist match · {plate}</p>
          {children ? <div className="mt-0.5 text-xs text-danger">{children}</div> : null}
        </div>
      </div>
      {action ? <div className="shrink-0">{action}</div> : null}
    </div>
  );
}

export default EmptyState;