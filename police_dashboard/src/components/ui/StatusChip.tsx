import type { ReactNode } from "react";

// One mapping shared by the citizen portal, the police dashboard and the
// mobile app, so a complaint never changes colour between screens.
//
// `chip` classes resolve to --chip-* tokens in index.css, which flip between
// a soft tint (light) and a translucent hue with light text (dark). Both stay
// at or above WCAG AA on their own background.
export const STATUS_STYLES: Record<string, { chip: string; dot: string; label: string }> = {
  PENDING: { chip: "chip-warning", dot: "bg-warning", label: "Pending" },
  UNDER_REVIEW: { chip: "chip-info", dot: "bg-info", label: "Under review" },
  VERIFIED: { chip: "chip-success", dot: "bg-success", label: "Verified" },
  REJECTED: { chip: "chip-danger", dot: "bg-danger", label: "Rejected" },
  HOTLISTED: { chip: "chip-primary", dot: "bg-primary-500", label: "Hotlisted" },
  CLOSED: { chip: "chip-neutral", dot: "bg-surface-muted", label: "Closed" },
};

export function statusStyle(status?: string | null) {
  const fallback = { chip: "chip-neutral", dot: "bg-surface-muted", label: "Unknown" };
  if (!status) return fallback;
  return STATUS_STYLES[status.toUpperCase()] || { ...fallback, label: status };
}

export function StatusChip({
  status,
  label,
  className = "",
}: {
  status?: string | null;
  label?: string;
  className?: string;
}) {
  const style = statusStyle(status);
  return (
    <span
      className={[
        "inline-flex items-center gap-1.5 rounded-full px-2.5 py-1",
        "text-xs font-medium whitespace-nowrap",
        style.chip,
        className,
      ].join(" ")}
    >
      <span className={["h-1.5 w-1.5 rounded-full", style.dot].join(" ")} aria-hidden />
      {label ?? style.label}
    </span>
  );
}

export function Badge({
  children,
  tone = "neutral",
  className = "",
}: {
  children: ReactNode;
  tone?: "neutral" | "primary" | "success" | "warning" | "danger" | "info";
  className?: string;
}) {
  const tones: Record<string, string> = {
    neutral: "chip-neutral",
    primary: "chip-primary",
    success: "chip-success",
    warning: "chip-warning",
    danger: "chip-danger",
    info: "chip-info",
  };
  return (
    <span
      className={[
        "inline-flex items-center rounded-md px-2 py-0.5 text-xs font-medium whitespace-nowrap",
        tones[tone],
        className,
      ].join(" ")}
    >
      {children}
    </span>
  );
}

/**
 * An Indian number plate rendered the way it looks on the vehicle.
 *
 * Deliberately hard-coded white-on-black in both themes: the plate badge is a
 * depiction of a physical object, so it must not invert with the UI.
 */
export function PlateBadge({
  plate,
  size = "md",
  className = "",
}: {
  plate: string;
  size?: "sm" | "md" | "lg";
  className?: string;
}) {
  const sizes = {
    sm: "text-xs px-2 py-0.5 tracking-wide",
    md: "text-sm px-2.5 py-1 tracking-wider",
    lg: "text-xl px-4 py-1.5 tracking-widest",
  };
  return (
    <span
      className={[
        "inline-flex items-center rounded-md border-2 border-slate-800 bg-white",
        "font-mono font-bold text-slate-900 shadow-sm",
        sizes[size],
        className,
      ].join(" ")}
    >
      {plate}
    </span>
  );
}

export default StatusChip;
