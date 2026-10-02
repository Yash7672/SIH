import { Monitor, Moon, Sun } from "lucide-react";
import { useTheme, type ThemeMode } from "../hooks/useTheme";

const OPTIONS: { value: ThemeMode; label: string; Icon: typeof Sun }[] = [
  { value: "light", label: "Light", Icon: Sun },
  { value: "dark", label: "Dark", Icon: Moon },
  { value: "system", label: "System", Icon: Monitor },
];

const BASE =
  "inline-flex items-center justify-center rounded-lg transition-colors duration-150 disabled:opacity-60";

/**
 * Quick sun/moon toggle for the top bar and the auth pages.
 *
 * Tapping it flips to the opposite of whatever is currently on screen, so the
 * button always reads as "switch me over". System mode resolves first, which
 * means a phone in system-dark shows the sun.
 */
export function ThemeToggle({ className = "" }: { className?: string }) {
  const { isDark, setMode } = useTheme();
  const label = isDark ? "Switch to light theme" : "Switch to dark theme";

  return (
    <button
      type="button"
      onClick={() => setMode(isDark ? "light" : "dark")}
      aria-label={label}
      title={label}
      aria-pressed={isDark}
      className={`${BASE} h-9 w-9 border border-surface-border bg-surface-card text-surface-muted hover:bg-surface-raised hover:text-surface-text ${className}`}
    >
      {isDark ? <Sun className="h-[18px] w-[18px]" aria-hidden /> : <Moon className="h-[18px] w-[18px]" aria-hidden />}
    </button>
  );
}

/** Full Light / Dark / System segmented control. Lives in the user menu. */
export function ThemeModePicker({ className = "" }: { className?: string }) {
  const { mode, setMode } = useTheme();

  return (
    <div
      role="radiogroup"
      aria-label="Colour theme"
      className={`inline-flex items-center gap-0.5 rounded-lg border border-surface-border bg-surface-raised p-0.5 ${className}`}
    >
      {OPTIONS.map(({ value, label, Icon }) => {
        const active = mode === value;
        return (
          <button
            key={value}
            type="button"
            role="radio"
            aria-checked={active}
            aria-label={`${label} theme`}
            title={`${label} theme`}
            onClick={() => setMode(value)}
            className={[
              BASE,
              "flex-1 gap-1.5 px-2 py-1.5 text-xs font-semibold",
              active
                ? "bg-surface-card text-primary-700 shadow-card dark:text-primary-200"
                : "text-surface-muted hover:text-surface-text",
            ].join(" ")}
          >
            <Icon className="h-3.5 w-3.5 shrink-0" aria-hidden />
            <span className="hidden sm:inline">{label}</span>
          </button>
        );
      })}
    </div>
  );
}

export default ThemeToggle;
