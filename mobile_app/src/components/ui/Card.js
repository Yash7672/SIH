import { StyleSheet, Text, View } from "react-native";
import { fontSize, fontWeight, radius, shadow, spacing } from "../../theme";
import { useTheme } from "../../theme/ThemeContext";

/**
 * Surface container.
 *
 * `tone` decides the palette:
 *   - "surface" follows the active theme
 *   - "dark"    the permanently-dark scanner chrome
 */
export function Card({ children, style, padded = true, tone = "surface" }) {
  const { colors } = useTheme();
  const dark = tone === "dark";
  return (
    <View
      style={[
        styles.card,
        {
          backgroundColor: dark ? colors.dark[800] : colors.surface.card,
          borderColor: dark ? colors.dark[600] : colors.surface.border,
        },
        padded && styles.padded,
        shadow.card,
        style,
      ]}
    >
      {children}
    </View>
  );
}

/** Small rounded label. Used for statuses, counts and metadata. */
export function Chip({ label, bg, fg, style, dot }) {
  return (
    <View style={[styles.chip, { backgroundColor: bg }, style]}>
      {dot ? <View style={[styles.chipDot, { backgroundColor: fg }]} /> : null}
      <Text style={[styles.chipText, { color: fg }]} numberOfLines={1}>
        {label}
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({
  card: {
    borderRadius: radius.lg,
    borderWidth: 1,
  },
  padded: { padding: spacing.lg },
  chip: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.xs,
    paddingHorizontal: spacing.md - 2,
    paddingVertical: 5,
    borderRadius: radius.pill,
    alignSelf: "flex-start",
  },
  chipDot: { width: 6, height: 6, borderRadius: 3 },
  chipText: { fontSize: fontSize.xs, fontWeight: fontWeight.semibold },
});