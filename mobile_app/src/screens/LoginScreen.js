import { useMemo, useState } from "react";
import {
  ActivityIndicator,
  KeyboardAvoidingView,
  Platform,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  TouchableOpacity,
  View,
} from "react-native";
import { login, register } from "../services/api";
import { saveSession } from "../storage/store";
import { useTheme } from "../theme/ThemeContext";
import { ThemeToggle } from "../components/ThemeToggle";
import WatermarkText from "../components/WatermarkText";

export default function LoginScreen({ onLogin }) {
  const { isDark, colors } = useTheme();
  const [mode, setMode] = useState("login");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [form, setForm] = useState({
    name: "",
    email: "volunteer@example.com",
    phone: "",
    password: "Volunteer@123",
  });

  // One stylesheet per theme, rebuilt only when the theme flips.
  const s = useMemo(() => makeStyles(colors, isDark), [colors, isDark]);

  function set(key, value) {
    setForm((f) => ({ ...f, [key]: value }));
  }

  async function submit() {
    setError("");
    setLoading(true);
    try {
      const resp =
        mode === "login"
          ? await login(form.email.trim(), form.password)
          : await register({ ...form, role: "VOLUNTEER" });
      // Persist the access + refresh pair so the scanner survives token expiry.
      await saveSession({
        token: resp.access_token,
        refreshToken: resp.refresh_token,
        user: resp.user,
      });
      onLogin(resp.user);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  const field = (key, props) => (
    <TextInput
      {...props}
      style={[s.input, props.style]}
      placeholderTextColor={colors.surface.muted}
      value={form[key]}
      onChangeText={(v) => set(key, v)}
    />
  );

  return (
    <KeyboardAvoidingView
      behavior={Platform.OS === "ios" ? "padding" : undefined}
      style={s.flex}
    >
      <View style={s.toggleSlot}>
        <ThemeToggle />
      </View>

      <ScrollView contentContainerStyle={s.container} keyboardShouldPersistTaps="handled">
        <WatermarkText />

        <Text style={s.title}>RAKSHAK</Text>
        <Text style={s.subtitle}>Privacy-first ANPR scanning</Text>

        <View style={s.card}>
          <View style={s.tabs}>
            {["login", "register"].map((m) => {
              const active = mode === m;
              return (
                <TouchableOpacity
                  key={m}
                  accessibilityRole="tab"
                  accessibilityState={{ selected: active }}
                  style={[s.tab, active && s.tabActive]}
                  onPress={() => {
                    setMode(m);
                    setError("");
                  }}
                >
                  <Text style={[s.tabText, active && s.tabTextActive]}>
                    {m === "login" ? "Sign in" : "New volunteer"}
                  </Text>
                </TouchableOpacity>
              );
            })}
          </View>

          {mode === "register" && field("name", { placeholder: "Full name" })}
          {field("email", {
            placeholder: "Email",
            autoCapitalize: "none",
            keyboardType: "email-address",
          })}
          {mode === "register" &&
            field("phone", { placeholder: "Phone (optional)", keyboardType: "phone-pad" })}
          {field("password", { placeholder: "Password", secureTextEntry: true })}

          {error ? (
            <View style={s.errorBox}>
              <Text style={s.error}>{error}</Text>
            </View>
          ) : null}

          <TouchableOpacity
            style={s.button}
            accessibilityRole="button"
            accessibilityState={{ disabled: loading, busy: loading }}
            onPress={submit}
            disabled={loading}
          >
            {loading ? (
              <ActivityIndicator color="#FFFFFF" />
            ) : (
              <Text style={s.buttonText}>
                {mode === "login" ? "Sign in" : "Register as volunteer"}
              </Text>
            )}
          </TouchableOpacity>

          <Text style={s.hint}>Demo volunteer: volunteer@example.com / Volunteer@123</Text>
        </View>
      </ScrollView>
    </KeyboardAvoidingView>
  );
}

function makeStyles(c, isDark) {
  return StyleSheet.create({
    flex: { flex: 1, backgroundColor: c.surface.bg },
    toggleSlot: {
      position: "absolute",
      top: Platform.OS === "ios" ? 56 : 44,
      right: 16,
      zIndex: 20,
    },
    container: { flexGrow: 1, justifyContent: "center", padding: 24 },
    title: {
      color: isDark ? "#E6EDF7" : "#0B1437",
      fontSize: 34,
      fontWeight: "800",
      textAlign: "center",
      letterSpacing: 2,
    },
    subtitle: {
      color: c.surface.muted,
      textAlign: "center",
      marginTop: 4,
      marginBottom: 28,
    },
    card: {
      backgroundColor: c.surface.card,
      borderRadius: 16,
      padding: 20,
      borderWidth: 1,
      borderColor: c.surface.border,
      zIndex: 1,
    },
    tabs: {
      flexDirection: "row",
      backgroundColor: c.surface.raised,
      borderRadius: 10,
      padding: 3,
      marginBottom: 16,
    },
    tab: { flex: 1, paddingVertical: 8, borderRadius: 8, alignItems: "center" },
    tabActive: { backgroundColor: isDark ? "#1B2742" : "#FFFFFF" },
    tabText: { color: c.surface.muted, fontWeight: "600" },
    tabTextActive: { color: isDark ? "#E6EDF7" : "#0F172A" },
    input: {
      backgroundColor: c.surface.raised,
      color: c.surface.text,
      borderRadius: 10,
      paddingVertical: 12,
      paddingHorizontal: 14,
      marginBottom: 12,
      borderWidth: 1,
      borderColor: c.surface.border,
    },
    errorBox: {
      backgroundColor: isDark ? "#4A1519" : "#FEE2E2",
      borderRadius: 10,
      borderWidth: 1,
      borderColor: isDark ? "#7F1D1D" : "#FCA5A5",
      padding: 10,
      marginBottom: 10,
    },
    button: {
      backgroundColor: c.primary[600],
      borderRadius: 10,
      paddingVertical: 14,
      alignItems: "center",
      marginTop: 4,
      zIndex: 1,
    },
    buttonText: { color: "#FFFFFF", fontWeight: "700" },
    error: { color: isDark ? "#FCA5A5" : "#7F1D1D", fontSize: 13 },
    hint: { color: c.surface.subtle, fontSize: 12, textAlign: "center", marginTop: 14 },
  });
}
