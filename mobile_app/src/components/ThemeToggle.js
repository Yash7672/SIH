import React from "react";
import { StyleSheet, Text, TouchableOpacity, View } from "react-native";
import { useTheme } from "../theme/ThemeContext";

/**
 * Light / Dark / System switcher.
 *
 * Icons are plain Unicode glyphs on purpose: this project ships no icon font
 * and @expo/vector-icons is not installed, so importing it would crash the app
 * at startup. The glyphs render from the system font on both platforms.
 */
const OPTIONS = [
  { value: "light", label: "Light", glyph: "☀" },
  { value: "dark", label: "Dark", glyph: "☾" },
  { value: "system", label: "System", glyph: "◐" },
];

/** Quick sun/moon toggle for a screen corner. `variant="icon"` (default). */
export function ThemeToggle({ style, tone = "surface" }) {
  const { isDark, setMode } = useTheme();
  const onDark = tone === "dark";
  const fg = onDark ? "#E6EDF7" : isDark ? "#E6EDF7" : "#0F172A";
  const idle = onDark ? "#94A3B8" : isDark ? "#94A3B8" : "#5A6B85";
  const next = isDark ? "light" : "dark";
  const label = isDark ? "Switch to light theme" : "Switch to dark theme";

  return (
    <TouchableOpacity
      onPress={() => setMode(next)}
      accessibilityRole="button"
      accessibilityLabel={label}
      style={[
        styles.iconBtn,
        { borderColor: onDark ? "#263552" : isDark ? "#263552" : "#E2E8F0" },
        style,
      ]}
    >
      <Text style={[styles.iconGlyph, { color: fg }]} accessibilityElementsHidden>
        {isDark ? "☀" : "☾"}
      </Text>
    </TouchableOpacity>
  );
}

/** Full three-option picker for a settings sheet. `variant="segmented"`. */
export function ThemeModePicker({ style }) {
  const { mode, setMode, isDark } = useTheme();

  return (
    <View
      accessibilityRole="radiogroup"
      accessibilityLabel="Colour theme"
      style={[
        styles.segmented,
        { backgroundColor: isDark ? "#1B2742" : "#F1F5F9", borderColor: isDark ? "#263552" : "#E2E8F0" },
        style,
      ]}
    >
      {OPTIONS.map(({ value, label, glyph }) => {
        const active = mode === value;
        return (
          <TouchableOpacity
            key={value}
            onPress={() => setMode(value)}
            accessibilityRole="radio"
            accessibilityState={{ selected: active }}
            accessibilityLabel={`${label} theme`}
            style={[
              styles.segmentBtn,
              active && { backgroundColor: isDark ? "#111A2E" : "#FFFFFF" },
            ]}
          >
            <Text style={{ color: active ? "#2F4BD8" : isDark ? "#94A3B8" : "#5A6B85" }}>{glyph}</Text>
            <Text
              style={[
                styles.segmentText,
                { color: active ? (isDark ? "#E6EDF7" : "#0F172A") : isDark ? "#94A3B8" : "#5A6B85" },
              ]}
            >
              {label}
            </Text>
          </TouchableOpacity>
        );
      })}
    </View>
  );
}

export default function ThemeSwitch(props) {
  return props.variant === "segmented" ? <ThemeModePicker {...props} /> : <ThemeToggle {...props} />;
}

const styles = StyleSheet.create({
  iconBtn: {
    width: 40,
    height: 40,
    borderRadius: 12,
    borderWidth: 1,
    alignItems: "center",
    justifyContent: "center",
  },
  iconGlyph: {
    fontSize: 18,
    lineHeight: 22,
  },
  segmented: {
    flexDirection: "row",
    borderRadius: 12,
    borderWidth: 1,
    padding: 3,
  },
  segmentBtn: {
    flex: 1,
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: 5,
    paddingVertical: 8,
    borderRadius: 10,
  },
  segmentText: {
    fontSize: 12,
    fontWeight: "600",
  },
});
