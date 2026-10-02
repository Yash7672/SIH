import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

export type ThemeMode = "light" | "dark" | "system";

interface ThemeContextValue {
  /** The user's choice: explicit light/dark, or "system". */
  mode: ThemeMode;
  setMode: (mode: ThemeMode) => void;
  /** The theme actually in effect (system resolved). */
  isDark: boolean;
  /** False until the stored preference has been read from localStorage. */
  ready: boolean;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

export const THEME_STORAGE_KEY = "rakshak-theme";

/** Where the browser chrome colour should sit in each theme. */
const THEME_COLOR = { light: "#F8FAFC", dark: "#0B1220" } as const;

function readStoredMode(): ThemeMode {
  try {
    const stored = localStorage.getItem(THEME_STORAGE_KEY);
    if (stored === "light" || stored === "dark" || stored === "system") return stored;
  } catch {
    /* private mode / disabled storage: fall through to the default */
  }
  return "system";
}

function prefersDark() {
  return typeof window !== "undefined" && typeof window.matchMedia === "function"
    ? window.matchMedia("(prefers-color-scheme: dark)").matches
    : false;
}

/**
 * Applies a theme to <html>.
 *
 * index.html already set the `.dark` class inline before React boots, so this
 * is only responsible for switching afterwards and for keeping
 * <meta name="theme-color"> in sync with the browser/OS chrome.
 */
export function applyTheme(isDark: boolean) {
  if (typeof document === "undefined") return;
  const root = document.documentElement;
  root.classList.toggle("dark", isDark);
  root.style.colorScheme = isDark ? "dark" : "light";
  const meta = document.querySelector<HTMLMetaElement>('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", THEME_COLOR[isDark ? "dark" : "light"]);
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [mode, setModeState] = useState<ThemeMode>("system");
  const [ready, setReady] = useState(false);

  // Read the stored preference once, then never write on the initial pass.
  useEffect(() => {
    setModeState(readStoredMode());
    setReady(true);
  }, []);

  const isDark = mode === "system" ? prefersDark() : mode === "dark";

  useEffect(() => {
    if (!ready) return;
    try {
      localStorage.setItem(THEME_STORAGE_KEY, mode);
    } catch {
      /* nothing we can do; the class is still applied for this session */
    }
  }, [mode, ready]);

  useEffect(() => {
    applyTheme(isDark);
  }, [isDark]);

  // In "system" mode the OS can change the theme while the app is open.
  useEffect(() => {
    if (mode !== "system" || typeof window.matchMedia !== "function") return undefined;
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => applyTheme(mq.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, [mode]);

  const setMode = useCallback((next: ThemeMode) => setModeState(next), []);
  const value = useMemo<ThemeContextValue>(
    () => ({ mode, setMode, isDark, ready }),
    [mode, setMode, isDark, ready]
  );

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeContext);
  if (!ctx) throw new Error("useTheme must be used inside <ThemeProvider>");
  return ctx;
}

export default ThemeProvider;
