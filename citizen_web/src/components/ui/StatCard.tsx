import type { ReactNode } from "react";
import { TrendingDown, TrendingUp } from "lucide-react";

const TONES = {
  primary: "chip-primary",
  success: "chip-success",
  warning: "chip-warning",
  danger: "chip-danger",
  info: "chip-info",
  neutral: "chip-neutral",
} as const;

export function StatCard({
  label,
  value,
  icon,
  trend,
  hint,
  tone = "primary",
}: {
  label: string;
  value: ReactNode;
  icon?: ReactNode;
  /** Percentage change; sign picks the colour, 0/absent is neutral. */
  trend?: number | null;
  hint?: ReactNode;
  tone?: keyof typeof TONES;
}) {
  const hasTrend = typeof trend === "number" && Number.isFinite(trend);
  const up = hasTrend && trend! > 0;
  const flat = hasTrend && trend === 0;

  return (
    <div className="rounded-xl border border-surface-border bg-surface-card p-5 shadow-card">
      <div className="flex items-start justify-between gap-3">
        <p className="text-sm font-medium text-surface-muted">{label}</p>
        {icon ? (
          <span className={["shrink-0 rounded-lg p-2", TONES[tone]].join(" ")} aria-hidden>
            {icon}
          </span>
        ) : null}
      </div>
      <p className="mt-3 text-3xl font-semibold tracking-tight text-surface-text">{value}</p>
      <div className="mt-2 flex items-center gap-2 text-xs">
        {hasTrend && !flat ? (
          <span
            className={[
              "inline-flex items-center gap-1 font-medium",
              up ? "text-success" : "text-danger",
            ].join(" ")}
          >
            {up ? <TrendingUp className="h-3.5 w-3.5" aria-hidden /> : <TrendingDown className="h-3.5 w-3.5" aria-hidden />}
            {up ? "+" : ""}
            {trend}%
          </span>
        ) : null}
        {hint ? <span className="text-surface-muted">{hint}</span> : null}
      </div>
    </div>
  );
}

export default StatCard;