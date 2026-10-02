import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { useColorScheme } from "react-native";
import AsyncStorage from "@react-native-async-storage/async-storage";
import { lightColors, darkColors } from "../theme";

const ThemeContext = createContext({
  mode: "system",
  setMode: () => {},
  isDark: false,
  ready: false,
  colors: lightColors,
});

/** Shared with the two web clients so one preference key drives all three. */
const THEME_KEY = "rakshak-theme";

export function ThemeProvider({ children }) {
  // useColorScheme subscribes to the OS setting, so "system" mode updates live
  // without an extra Appearance listener.
  const systemScheme = useColorScheme();
  const [mode, setModeState] = useState("system");
  const [ready, setReady] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const stored = await AsyncStorage.getItem(THEME_KEY);
        if (!cancelled && (stored === "light" || stored === "dark" || stored === "system")) {
          setModeState(stored);
        }
      } catch {
        // storage unavailable: fall through to the "system" default
      } finally {
        if (!cancelled) setReady(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!ready) return;
    AsyncStorage.setItem(THEME_KEY, mode).catch(() => {});
  }, [mode, ready]);

  const isDark = mode === "dark" || (mode === "system" && systemScheme === "dark");
  const colors = isDark ? darkColors : lightColors;

  const setMode = useCallback((next) => setModeState(next), []);
  const value = useMemo(
    () => ({ mode, setMode, isDark, ready, colors }),
    [mode, setMode, isDark, ready, colors]
  );

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme() {
  return useContext(ThemeContext);
}

export default ThemeProvider;
