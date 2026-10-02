/**
 * Small formatting helpers shared by the console pages. Pure functions only,
 * so pages can memoise on the raw value without pulling in a date library.
 */

/** "just now" / "4 min ago" / "3 hr ago" / "12 Aug" */
export function timeAgo(input?: string | number | Date | null): string {
  if (!input) return "—";
  const then = new Date(input).getTime();
  if (Number.isNaN(then)) return "—";

  const seconds = Math.round((Date.now() - then) / 1000);
  if (seconds < 0) return new Date(input).toLocaleTimeString();
  if (seconds < 45) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} hr ago`;
  const days = Math.round(hours / 24);
  if (days < 7) return `${days} day${days === 1 ? "" : "s"} ago`;
  return new Date(input).toLocaleDateString();
}

export function formatCoords(lat?: number | string | null, lng?: number | string | null): string {
  const la = Number(lat);
  const ln = Number(lng);
  if (!Number.isFinite(la) || !Number.isFinite(ln)) return "Location unavailable";
  return `${la.toFixed(5)}, ${ln.toFixed(5)}`;
}

/** Turns free text into a URL-safe `/vehicles/ABC123` segment. */
export function platePath(plate: string): string {
  return `/vehicles/${encodeURIComponent(plate.replace(/\s+/g, "").toUpperCase())}`;
}
