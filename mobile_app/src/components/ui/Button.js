import { ActivityIndicator, Pressable, StyleSheet, Text, View } from "react-native";
import { fontSize, fontWeight, radius, spacing } from "../../theme";
import { useTheme } from "../../theme/ThemeContext";

const SIZES = {
  sm: { paddingVertical: spacing.sm, paddingHorizontal: spacing.md, text: fontSize.sm, minHeight: 36 },
  md: { paddingVertical: spacing.md, paddingHorizontal: spacing.lg, text: fontSize.base, minHeight: 48 },
  lg: { paddingVertical: 14, paddingHorizontal: spacing.xl, text: fontSize.lg, minHeight: 56 },
};

/**
 * The one button in the app. Every interactive control is a Pressable so the
 * pressed state is identical on Android and iOS.
 *
 * `appearance` picks the palette the button is drawn for:
 *   - "dark"    always-dark chrome (the scanner, its sheets and menus)
 *   - "surface" follows the active theme (login, permission prompt)
 *
 * Defaults to "dark" because the scanner is the only place most buttons live.
 */
export default function Button({
  label,
  onPress,
  variant = "primary",
  size = "md",
  appearance = "dark",
  loading = false,
  disabled = false,
  loadingLabel,
  icon = null,
  style,
  textStyle,
  accessibilityLabel,
  testID,
}) {
  const { colors } = useTheme();
  const s = SIZES[size] || SIZES.md;
  const isDisabled = disabled || loading;

  const dark = appearance === "dark";
  const v = dark
    ? {
        primary: { bg: colors.solid, pressed: colors.solidHover, fg: "#FFFFFF", border: "transparent" },
        secondary: { bg: colors.dark[700], pressed: colors.dark[600], fg: colors.onDark.body, border: colors.dark[500] },
        danger: { bg: colors.solidDanger, pressed: colors.solidDangerHover, fg: "#FFFFFF", border: "transparent" },
        ghost: { bg: "transparent", pressed: colors.dark[700], fg: colors.onDark.muted, border: "transparent" },
      }
    : {
        primary: { bg: colors.solid, pressed: colors.solidHover, fg: "#FFFFFF", border: "transparent" },
        secondary: {
          bg: colors.surface.card,
          pressed: colors.surface.raised,
          fg: colors.surface.text,
          border: colors.surface.border,
        },
        danger: { bg: colors.solidDanger, pressed: colors.solidDangerHover, fg: "#FFFFFF", border: "transparent" },
        ghost: { bg: "transparent", pressed: colors.surface.raised, fg: colors.surface.muted, border: "transparent" },
      };
  const spec = v[variant] || v.primary;

  return (
    <Pressable
      testID={testID}
      accessibilityRole="button"
      accessibilityLabel={accessibilityLabel || label}
      accessibilityState={{ disabled: isDisabled, busy: loading }}
      onPress={isDisabled ? undefined : onPress}
      style={({ pressed }) => [
        styles.base,
        {
          backgroundColor: pressed && !isDisabled ? spec.pressed : spec.bg,
          borderColor: spec.border,
          paddingVertical: s.paddingVertical,
          paddingHorizontal: s.paddingHorizontal,
          minHeight: s.minHeight,
          opacity: isDisabled ? 0.55 : 1,
          transform: [{ scale: pressed && !isDisabled ? 0.98 : 1 }],
        },
        style,
      ]}
    >
      {loading ? (
        <View style={styles.loadingRow}>
          <ActivityIndicator size="small" color={spec.fg} />
          {loadingLabel ? <Text style={[styles.text, { color: spec.fg, fontSize: s.text }]}>{loadingLabel}</Text> : null}
        </View>
      ) : (
        <View style={styles.contentRow}>
          {icon ? <View style={styles.icon}>{icon}</View> : null}
          <Text style={[styles.text, { color: spec.fg, fontSize: s.text }, textStyle]} numberOfLines={1}>
            {label}
          </Text>
        </View>
      )}
    </Pressable>
  );
}

const styles = StyleSheet.create({
  base: {
    borderRadius: radius.md,
    borderWidth: 1,
    alignItems: "center",
    justifyContent: "center",
  },
  text: {
    fontWeight: fontWeight.bold,
    letterSpacing: 0.2,
  },
  loadingRow: { flexDirection: "row", alignItems: "center", gap: spacing.sm },
  contentRow: { flexDirection: "row", alignItems: "center", gap: spacing.sm },
  icon: { marginRight: 2 },
});