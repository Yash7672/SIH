import type { ReactNode } from "react";

/**
 * Hotlist lifecycle chips (ACTIVE / FIR_CONFIRMED / RECOVERED / CLOSED /
 * EXPIRED). Kept separate from the complaint status mapping in
 * components/ui/StatusChip.tsx: a complaint status and a hotlist status mean
 * different things even when the words look similar.
 */
export const HOTLIST_STATUS_STYLES: Record<string, { chip: string; dot: string; label: string }> = {
  ACTIVE: { chip: "chip-danger", dot: "bg-danger", label: "Active" },
  FIR_CONFIRMED: { chip: "chip-warning", dot: "bg-warning", label: "FIR confirmed" },
  RECOVERED: { chip: "chip-success", dot: "bg-success", label: "Recovered" },
  CLOSED: { chip: "chip-neutral", dot: "bg-surface-muted", label: "Closed" },
  EXPIRED: { chip: "chip-neutral", dot: "bg-surface-subtle", label: "Expired" },
};

export function HotlistChip({
  status,
  label,
  className = "",
}: {
  status?: string | null;
  label?: ReactNode;
  className?: string;
}) {
  const key = (status || "").toUpperCase();
  const fallback = { chip: "chip-neutral", dot: "bg-surface-muted", label: "Unknown" };
  const style =
    HOTLIST_STATUS_STYLES[key] || { ...fallback, label: status || "Unknown" };
  return (
    <span
      className={[
        "inline-flex items-center gap-1.5 whitespace-nowrap rounded-full px-2.5 py-1",
        "text-xs font-medium",
        style.chip,
        className,
      ].join(" ")}
    >
      <span className={["h-1.5 w-1.5 rounded-full", style.dot].join(" ")} aria-hidden />
      {label ?? style.label}
    </span>
  );
}

export default HotlistChip;
