import { useCallback, useEffect, useMemo, useState } from "react";
import { ActivityIndicator, StyleSheet, View } from "react-native";
import { StatusBar } from "expo-status-bar";
import { SafeAreaProvider } from "react-native-safe-area-context";
import LoginScreen from "./src/screens/LoginScreen";
import ScannerScreen from "./src/screens/ScannerScreen";
import { clearSession, loadSession } from "./src/storage/store";
import { setTokens } from "./src/services/api";
import { ThemeProvider, useTheme } from "./src/theme/ThemeContext";

function AppInner() {
  const { isDark, ready: themeReady, colors } = useTheme();
  const [sessionLoaded, setSessionLoaded] = useState(false);
  const [user, setUser] = useState(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const { token, refreshToken, user: u } = await loadSession();
      if (cancelled) return;
      if (token) setTokens({ token, refreshToken });
      setUser(u);
      setSessionLoaded(true);
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const onLogin = useCallback((nextUser) => setUser(nextUser), []);

  const onLogout = useCallback(async () => {
    await clearSession();
    setTokens({ token: null, refreshToken: null });
    setUser(null);
  }, []);

  const styles = useMemo(
    () =>
      StyleSheet.create({
        root: { flex: 1, backgroundColor: colors.surface.bg },
        center: {
          flex: 1,
          alignItems: "center",
          justifyContent: "center",
          backgroundColor: colors.surface.bg,
        },
      }),
    [colors]
  );

  // Hold the splash until BOTH the stored theme and the stored session have
  // been read, otherwise the first screen would render in the wrong palette.
  if (!themeReady || !sessionLoaded) {
    return (
      <View style={styles.center}>
        <StatusBar style={isDark ? "light" : "dark"} />
        <ActivityIndicator color="#2F4BD8" size="large" />
      </View>
    );
  }

  return (
    <View style={styles.root}>
      <StatusBar style={isDark ? "light" : "dark"} />
      {user ? (
        <ScannerScreen user={user} onLogout={onLogout} />
      ) : (
        <LoginScreen onLogin={onLogin} />
      )}
    </View>
  );
}

export default function App() {
  return (
    // SDK 57 on Android draws edge to edge, so every screen has to apply the
    // status-bar / navigation-bar insets itself. SafeAreaProvider measures them
    // once for the whole tree; useSafeAreaInsets() reads them per screen.
    <SafeAreaProvider>
      <ThemeProvider>
        <AppInner />
      </ThemeProvider>
    </SafeAreaProvider>
  );
}
