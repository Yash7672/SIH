import { useEffect, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";

export function PageHeader({
  title,
  subtitle,
  action,
  className = "",
}: {
  title: string;
  subtitle?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div className={["flex flex-wrap items-start justify-between gap-4", className].join(" ")}>
      <div className="min-w-0">
        <h1 className="text-2xl font-semibold tracking-tight text-surface-text">{title}</h1>
        {subtitle ? <p className="mt-1 text-sm text-surface-muted">{subtitle}</p> : null}
      </div>
      {action ? <div className="shrink-0">{action}</div> : null}
    </div>
  );
}

export function Modal({
  open,
  onClose,
  title,
  description,
  children,
  footer,
  size = "md",
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  description?: ReactNode;
  children?: ReactNode;
  footer?: ReactNode;
  size?: "sm" | "md" | "lg";
}) {
  const panelRef = useRef<HTMLDivElement>(null);

  // Escape closes, and body scroll locks behind the overlay so a long table
  // underneath cannot scroll the dialog out of view.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    panelRef.current?.focus();
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = previous;
    };
  }, [open, onClose]);

  if (!open) return null;

  const widths = { sm: "max-w-sm", md: "max-w-md", lg: "max-w-2xl" };

  return createPortal(
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-primary-900/50 p-4 backdrop-blur-sm sm:items-center">
      <button
        type="button"
        aria-label="Close dialog"
        className="absolute inset-0 cursor-default"
        onClick={onClose}
      />
      <div
        ref={panelRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className={[
          "relative w-full animate-slide-up rounded-2xl bg-surface-card shadow-overlay outline-none",
          widths[size],
        ].join(" ")}
      >
        <div className="flex items-start justify-between gap-4 border-b border-surface-border p-5">
          <div className="min-w-0">
            <h2 className="text-base font-semibold text-surface-text">{title}</h2>
            {description ? <p className="mt-1 text-sm text-surface-muted">{description}</p> : null}
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="-mr-1 shrink-0 rounded-md p-1.5 text-surface-muted transition-colors hover:bg-surface-raised hover:text-surface-text"
          >
            <X className="h-4 w-4" aria-hidden />
          </button>
        </div>
        {children ? <div className="p-5">{children}</div> : null}
        {footer ? (
          <div className="flex justify-end gap-3 border-t border-surface-border p-5">{footer}</div>
        ) : null}
      </div>
    </div>,
    document.body
  );
}

export type ToastTone = "success" | "danger" | "info" | "warning";

export function Toast({
  open,
  tone = "success",
  message,
  onClose,
}: {
  open: boolean;
  tone?: ToastTone;
  message: string;
  onClose?: () => void;
}) {
  useEffect(() => {
    if (!open || !onClose) return;
    const t = setTimeout(onClose, 4000);
    return () => clearTimeout(t);
  }, [open, onClose]);

  if (!open) return null;

  // Chip tokens keep these readable in both themes without hand-picked pairs.
  const tones: Record<ToastTone, string> = {
    success: "border-success/40 chip-success",
    danger: "border-danger/40 chip-danger",
    info: "border-info/40 chip-info",
    warning: "border-warning/40 chip-warning",
  };

  return createPortal(
    <div
      role="status"
      aria-live="polite"
      className="fixed inset-x-4 bottom-4 z-50 flex justify-center sm:left-auto sm:right-6 sm:justify-end"
    >
      <div
        className={[
          "flex max-w-sm animate-slide-up items-center gap-3 rounded-lg border px-4 py-3 text-sm font-medium shadow-overlay",
          tones[tone],
        ].join(" ")}
      >
        <span className="flex-1">{message}</span>
        {onClose ? (
          <button type="button" onClick={onClose} aria-label="Dismiss" className="opacity-60 hover:opacity-100">
            <X className="h-4 w-4" aria-hidden />
          </button>
        ) : null}
      </div>
    </div>,
    document.body
  );
}

export default Modal;