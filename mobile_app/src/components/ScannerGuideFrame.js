import { memo, useEffect, useRef } from "react";
import { Animated, Easing, StyleSheet, Text, View, useWindowDimensions } from "react-native";
import { colors, fontSize, fontWeight, radius, spacing } from "../theme";

/**
 * The viewfinder guide: a plate-shaped frame with animated corner brackets,
 * and the area outside it dimmed so the eye lands on the plate.
 *
 * Memoised and driven entirely by Animated (native driver), so the pulsing
 * brackets never re-render the camera underneath.
 */

// Indian plates are 3:1; 2.6:1 leaves room for the frame's own stroke.
const ASPECT = 2.6;

function Corner({ position, pulse }) {
  const style = [
    styles.corner,
    position,
    { opacity: pulse.interpolate({ inputRange: [0, 1], outputRange: [0.45, 1] }) },
  ];
  return <Animated.View style={style} />;
}

function ScannerGuideFrameBase({ scanning, hint, containerWidth, containerHeight }) {
  const window = useWindowDimensions();

  // The overlay is absolutely positioned inside the camera container, so it has
  // to be laid out against THAT box. Sizing it against the window put the frame
  // ~100px below the bottom of the viewfinder and let it cover the debug card.
  const width = containerWidth || window.width;
  const height = containerHeight || window.height;

  // Frame width adapts to the viewport so it is usable on a 5" phone and does
  // not look lost on a tablet.
  const frameWidth = Math.min(width - spacing.xl * 2, width * 0.88, 420);
  const frameHeight = Math.round(frameWidth / ASPECT);
  const stroke = 3;
  const bracket = Math.max(24, Math.round(frameWidth * 0.11));

  const pulse = useRef(new Animated.Value(0)).current;

  useEffect(() => {
    if (scanning) {
      pulse.setValue(1);
      return undefined;
    }
    const animation = Animated.loop(
      Animated.sequence([
        Animated.timing(pulse, {
          toValue: 1,
          duration: 1100,
          easing: Easing.inOut(Easing.ease),
          useNativeDriver: true,
        }),
        Animated.timing(pulse, {
          toValue: 0.35,
          duration: 1100,
          easing: Easing.inOut(Easing.ease),
          useNativeDriver: true,
        }),
      ])
    );
    animation.start();
    return () => animation.stop();
  }, [pulse, scanning]);

  const left = (width - frameWidth) / 2;
  // Nudged above centre so the hint strip at the bottom of the preview has room.
  const top = Math.max((height - frameHeight) / 2 - height * 0.08, spacing.xs);

  return (
    <View pointerEvents="none" style={StyleSheet.absoluteFill}>
      {/* Dim everything outside the frame with four views instead of a mask -
          no extra native dependency, and it renders identically on Android. */}
      <View style={[styles.scrim, { left: 0, right: 0, top: 0, height: top }]} />
      <View style={[styles.scrim, { left: 0, right: 0, top: top + frameHeight, bottom: 0 }]} />
      <View style={[styles.scrim, { left: 0, width: left, top, height: frameHeight }]} />
      <View style={[styles.scrim, { left: left + frameWidth, right: 0, top, height: frameHeight }]} />

      <View style={[styles.frame, { left, top, width: frameWidth, height: frameHeight }]}>
        <View style={[styles.plateEdge, { borderRadius: radius.sm }]} />
        <Corner position={[styles.tl, { width: bracket, height: bracket, borderWidth: stroke }]} pulse={pulse} />
        <Corner position={[styles.tr, { width: bracket, height: bracket, borderWidth: stroke }]} pulse={pulse} />
        <Corner position={[styles.bl, { width: bracket, height: bracket, borderWidth: stroke }]} pulse={pulse} />
        <Corner position={[styles.br, { width: bracket, height: bracket, borderWidth: stroke }]} pulse={pulse} />

        {scanning ? (
          <View style={[styles.scanLine, { top: Math.round(frameHeight / 2) }]} />
        ) : null}
      </View>

      {hint ? (
        <View style={[styles.hintWrap, { top: top + frameHeight + spacing.lg }]}>
          <Text style={styles.hint} numberOfLines={2}>
            {hint}
          </Text>
        </View>
      ) : null}
    </View>
  );
}

export const ScannerGuideFrame = memo(ScannerGuideFrameBase);

const BRACKET_COLOR = colors.accent[400];

const styles = StyleSheet.create({
  scrim: { position: "absolute", backgroundColor: "rgba(4,8,20,0.52)" },
  frame: { position: "absolute" },
  // A faint inner plate shape so the 3:1 target reads even before the corners
  // finish their pulse.
  plateEdge: {
    flex: 1,
    borderWidth: 1,
    borderColor: "rgba(255,255,255,0.28)",
    borderStyle: "dashed",
  },
  corner: { position: "absolute", borderColor: BRACKET_COLOR },
  tl: { top: -1, left: -1, borderRightWidth: 0, borderBottomWidth: 0, borderTopLeftRadius: radius.sm },
  tr: { top: -1, right: -1, borderLeftWidth: 0, borderBottomWidth: 0, borderTopRightRadius: radius.sm },
  bl: { bottom: -1, left: -1, borderRightWidth: 0, borderTopWidth: 0, borderBottomLeftRadius: radius.sm },
  br: { bottom: -1, right: -1, borderLeftWidth: 0, borderTopWidth: 0, borderBottomRightRadius: radius.sm },
  scanLine: {
    position: "absolute",
    left: 6,
    right: 6,
    height: 2,
    backgroundColor: colors.accent[300],
    shadowColor: colors.accent[400],
    shadowOpacity: 0.9,
    shadowRadius: 6,
    elevation: 4,
  },
  hintWrap: { position: "absolute", left: spacing.xl, right: spacing.xl, alignItems: "center" },
  hint: {
    color: colors.onDark.strong,
    fontSize: fontSize.sm,
    fontWeight: fontWeight.medium,
    textAlign: "center",
    backgroundColor: "rgba(11,18,32,0.7)",
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.sm,
    borderRadius: radius.pill,
    overflow: "hidden",
  },
});
