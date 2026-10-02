import type { HTMLAttributes, ReactNode } from "react";

interface CardProps extends HTMLAttributes<HTMLDivElement> {
  padding?: "none" | "sm" | "md";
}

const PADDING = {
  none: "",
  sm: "p-4",
  md: "p-6",
};

export function Card({
  padding = "md",
  className = "",
  children,
  ...rest
}: CardProps) {
  return (
    <div
      className={[
        "rounded-xl border border-surface-border bg-surface-card shadow-card",
        PADDING[padding],
        className,
      ].join(" ")}
      {...rest}
    >
      {children}
    </div>
  );
}

export function CardHeader({
  title,
  subtitle,
  action,
  className = "",
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div className={["flex items-start justify-between gap-4", className].join(" ")}>
      <div className="min-w-0">
        <h2 className="truncate text-base font-semibold text-surface-text">{title}</h2>
        {subtitle ? (
          <p className="mt-0.5 text-sm text-surface-muted">{subtitle}</p>
        ) : null}
      </div>
      {action ? <div className="shrink-0">{action}</div> : null}
    </div>
  );
}

export default Card;