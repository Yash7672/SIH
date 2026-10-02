import { ActivityIndicator, Pressable, StyleSheet, Text, View } from "react-native";
import { fontSize, fontWeight, radius, spacing } from "../../theme";
import { useTheme } from "../../theme/ThemeContext";

/**
 * Inline banner. Used for scan errors, network problems and the session-expired
 * notice - each needs its own colour so they read as different problems.
 *
 * `appearance="dark"` (default) renders the soft tint used on the scanner's
 * permanently-dark chrome; `"surface"` renders the tint that stays legible on
 * a themed surface such as the login screen.
 */
export function Banner({ tone = "info", title, message, actionLabel, onAction, style, appearance = "dark" }) {
  const { colors } = useTheme();
  if (!title && !message) return null;

  const dark = appearance === "dark";
  const t =
    (dark
      ? {
          info: { bg: "rgba(56,189,248,0.16)", border: "rgba(56,189,248,0.45)", fg: "#7DD3FC" },
          success: { bg: "rgba(74,222,128,0.16)", border: "rgba(74,222,128,0.45)", fg: "#86EFAC" },
          warning: { bg: "rgba(251,191,36,0.16)", border: "rgba(251,191,36,0.45)", fg: "#FCD34D" },
          danger: { bg: "rgba(220,38,38,0.18)", border: "rgba(220,38,38,0.5)", fg: "#FCA5A5" },
        }
      : {
          info: { bg: "rgba(3,105,161,0.10)", border: "rgba(3,105,161,0.40)", fg: "#075985" },
          success: { bg: "rgba(21,128,61,0.10)", border: "rgba(21,128,61,0.40)", fg: "#14532D" },
          warning: { bg: "rgba(180,83,9,0.10)", border: "rgba(180,83,9,0.40)", fg: "#78350F" },
          danger: { bg: "rgba(220,38,38,0.10)", border: "rgba(220,38,38,0.40)", fg: "#7F1D1D" },
        })[tone] || null;

  const bodyColor = dark ? colors.onDark.body : colors.surface.muted;

  return (
    <View
      accessibilityRole="alert"
      style={[styles.banner, { backgroundColor: t.bg, borderColor: t.border }, style]}
    >
      <View style={styles.body}>
        {title ? <Text style={[styles.title, { color: t.fg }]}>{title}</Text> : null}
        {message ? <Text style={[styles.message, { color: bodyColor }]}>{message}</Text> : null}
      </View>
      {actionLabel ? (
        <Pressable
          accessibilityRole="button"
          onPress={onAction}
          style={({ pressed }) => [styles.action, { opacity: pressed ? 0.7 : 1 }]}
        >
          <Text style={[styles.actionText, { color: t.fg }]}>{actionLabel}</Text>
        </Pressable>
      ) : null}
    </View>
  );
}

/** Full-screen placeholder used while the session or the camera warms up. */
export function LoadingState({ label = "Loading…", style, appearance = "dark" }) {
  const { colors } = useTheme();
  const dark = appearance === "dark";
  return (
    <View style={[styles.loading, { backgroundColor: dark ? colors.dark[900] : colors.surface.bg }, style]}>
      <ActivityIndicator size="large" color={dark ? colors.accent[400] : colors.primary[600]} />
      {label ? (
        <Text style={[styles.loadingText, { color: dark ? colors.onDark.muted : colors.surface.muted }]}>{label}</Text>
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  banner: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.md,
    borderWidth: 1,
    borderRadius: radius.md,
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.md - 2,
  },
  body: { flex: 1, gap: 2 },
  title: { fontSize: fontSize.sm, fontWeight: fontWeight.bold },
  message: { fontSize: fontSize.sm, lineHeight: 19 },
  action: { paddingHorizontal: spacing.sm, paddingVertical: spacing.xs },
  actionText: { fontSize: fontSize.sm, fontWeight: fontWeight.bold },
  loading: { flex: 1, alignItems: "center", justifyContent: "center", gap: spacing.md },
  loadingText: { fontSize: fontSize.sm },
});