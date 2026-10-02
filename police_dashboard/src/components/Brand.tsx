import { ShieldCheck } from "lucide-react";

/** The RAKSHAK shield mark. Inline SVG so no icon font or image request. */
export function ShieldMark({ className = "h-8 w-8" }: { className?: string }) {
  return (
    <span
      className={[
        "inline-flex items-center justify-center rounded-lg",
        "bg-gradient-to-br from-primary-600 to-primary-800 text-white",
        className,
      ].join(" ")}
      aria-hidden
    >
      <ShieldCheck className="h-1/2 w-1/2" strokeWidth={2.5} />
    </span>
  );
}

export function Wordmark({
  subtitle,
  tone = "light",
  className = "",
}: {
  subtitle?: string;
  /** dark = for the police console's dark chrome */
  tone?: "light" | "dark";
  className?: string;
}) {
  const title = tone === "dark" ? "text-white" : "text-primary-800";
  const sub = tone === "dark" ? "text-dark-600" : "text-surface-muted";
  return (
    <div className={["flex items-center gap-2.5", className].join(" ")}>
      <ShieldMark />
      <div className="leading-tight">
        <p className={["text-base font-bold tracking-tight", title].join(" ")}>RAKSHAK</p>
        {subtitle ? (
          <p className={["text-[10px] font-medium uppercase tracking-[0.14em]", sub].join(" ")}>
            {subtitle}
          </p>
        ) : null}
      </div>
    </div>
  );
}

export default Wordmark;