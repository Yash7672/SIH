import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from "react";
import { Loader2 } from "lucide-react";

type Variant = "primary" | "secondary" | "danger" | "ghost";
type Size = "sm" | "md" | "lg";

const VARIANTS: Record<Variant, string> = {
  // `solid` tokens are dark enough for white text in BOTH themes, unlike
  // `primary-600`, which flips so links stay AA on the current background.
  primary:
    "bg-solid text-white shadow-sm hover:bg-solid-hover active:bg-solid-hover disabled:hover:bg-solid",
  secondary:
    "bg-surface-card text-surface-text border border-surface-border shadow-sm hover:bg-surface-raised active:bg-surface-raised",
  danger:
    "bg-solid-danger text-white shadow-sm hover:bg-solid-danger-hover active:bg-solid-danger-hover disabled:hover:bg-solid-danger",
  ghost: "bg-transparent text-surface-muted hover:bg-surface-raised hover:text-surface-text",
};

const SIZES: Record<Size, string> = {
  sm: "h-8 px-3 text-xs gap-1.5 rounded-md",
  md: "h-10 px-4 text-sm gap-2 rounded-lg",
  lg: "h-12 px-6 text-base gap-2 rounded-lg",
};

interface Props extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
  loading?: boolean;
  fullWidth?: boolean;
  icon?: ReactNode;
}

export const Button = forwardRef<HTMLButtonElement, Props>(function Button(
  {
    variant = "primary",
    size = "md",
    loading = false,
    fullWidth = false,
    icon,
    className = "",
    children,
    disabled,
    type = "button",
    ...rest
  },
  ref
) {
  return (
    <button
      ref={ref}
      type={type}
      // A loading button must not be re-submittable, and keeping the label
      // in the DOM preserves the button width instead of collapsing it.
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      className={[
        "inline-flex items-center justify-center font-medium transition-colors",
        "disabled:cursor-not-allowed disabled:opacity-60",
        VARIANTS[variant],
        SIZES[size],
        fullWidth ? "w-full" : "",
        className,
      ].join(" ")}
      {...rest}
    >
      {loading ? (
        <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
      ) : (
        icon
      )}
      {children}
    </button>
  );
});

export default Button;