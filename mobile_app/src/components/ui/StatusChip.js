import { StyleSheet, Text, View } from "react-native";
import { fontSize, fontWeight, radius, spacing, statusTokens } from "../../theme";
import { useTheme } from "../../theme/ThemeContext";

/**
 * Complaint status chip. The mapping is the same one the two web clients use,
 * so a status never changes colour between screens; only the chip tint follows
 * the theme.
 */
export function StatusChip({ status, style }) {
  const { isDark, colors } = useTheme();
  const key = (status || "").toUpperCase();
  const tokens = statusTokens(isDark);
  const token = tokens[key] || {
    bg: isDark ? colors.dark[600] : colors.surface.raised,
    text: isDark ? colors.onDark.body : colors.surface.text,
    label: status || "Unknown",
  };
  return (
    <View style={[styles.chip, { backgroundColor: token.bg }, style]}>
      <Text style={[styles.text, { color: token.text }]}>{token.label}</Text>
    </View>
  );
}

/** Live connection / health pill with a coloured dot. */
export function ConnectionDot({ connected, label, style, tone = "dark" }) {
  const { colors } = useTheme();
  const color = connected ? colors.success : colors.danger;
  const fg = connected ? (tone === "dark" ? colors.onDark.body : colors.surface.text) : color;
  return (
    <View style={[styles.row, style]}>
      <View style={[styles.dot, { backgroundColor: color }]} />
      <Text style={[styles.rowText, { color: fg }]}>{label}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  chip: {
    paddingHorizontal: spacing.md - 2,
    paddingVertical: 4,
    borderRadius: radius.pill,
    alignSelf: "flex-start",
  },
  text: { fontSize: fontSize.xs, fontWeight: fontWeight.semibold },
  row: { flexDirection: "row", alignItems: "center", gap: spacing.xs + 2 },
  dot: { width: 8, height: 8, borderRadius: 4 },
  rowText: { fontSize: fontSize.xs, fontWeight: fontWeight.semibold },
});