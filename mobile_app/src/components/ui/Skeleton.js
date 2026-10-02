import { useEffect, useRef } from "react";
import { Animated, Easing, StyleSheet, View } from "react-native";
import { radius } from "../../theme";
import { useTheme } from "../../theme/ThemeContext";

/**
 * Pulsing placeholder block. `delay` staggers a list of them so a loading
 * screen looks like content arriving rather than a frozen page.
 */
export default function Skeleton({
  width = "100%",
  height = 16,
  radius: r = radius.sm,
  style,
  delay = 0,
  appearance = "dark",
}) {
  const { colors } = useTheme();
  const pulse = useRef(new Animated.Value(0.45)).current;

  useEffect(() => {
    const animation = Animated.loop(
      Animated.sequence([
        Animated.timing(pulse, {
          toValue: 1,
          duration: 750,
          delay,
          easing: Easing.inOut(Easing.ease),
          useNativeDriver: true,
        }),
        Animated.timing(pulse, {
          toValue: 0.45,
          duration: 750,
          easing: Easing.inOut(Easing.ease),
          useNativeDriver: true,
        }),
      ])
    );
    animation.start();
    // Stop the loop on unmount: a detached animation keeps the JS thread busy.
    return () => animation.stop();
  }, [pulse, delay]);

  return (
    <Animated.View
      accessibilityElementsHidden
      importantForAccessibility="no-hide-descendants"
      style={[
        {
          width,
          height,
          borderRadius: r,
          backgroundColor: appearance === "dark" ? colors.dark[600] : colors.surface.raised,
          opacity: pulse,
        },
        style,
      ]}
    >
      <View style={styles.inner} />
    </Animated.View>
  );
}

const styles = StyleSheet.create({
  inner: { flex: 1 },
});