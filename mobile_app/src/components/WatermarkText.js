import React, { memo } from "react";
import { StyleSheet, Text, View } from "react-native";
import { useTheme } from "../theme/ThemeContext";

/**
 * Centred, low-opacity "POLICE" watermark.
 *
 * Absolutely positioned behind the content, never receives touches and never
 * affects layout. Memoised so switching screens does not redraw it.
 *
 * Deliberately NOT used on the camera view: text over the viewfinder can lower
 * plate-read accuracy.
 */
function WatermarkText({ text = "POLICE" }) {
  const { isDark } = useTheme();

  return (
    <View pointerEvents="none" style={styles.container}>
      <Text
        style={[
          styles.text,
          { color: "#7DC4FF", opacity: isDark ? 0.07 : 0.08 },
        ]}
      >
        {text}
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    ...StyleSheet.absoluteFillObject,
    alignItems: "center",
    justifyContent: "center",
    zIndex: 0,
  },
  text: {
    fontSize: 64,
    fontWeight: "800",
    letterSpacing: 8,
    // Trailing letter-spacing pushes the word off-centre; pull it back.
    marginRight: -8,
    lineHeight: 72,
    textTransform: "uppercase",
  },
});

export default memo(WatermarkText);
