import React, { memo, useEffect, useMemo, useRef, useState } from "react";
import { Animated, StyleSheet, Text, View } from "react-native";
import { BOX_FADE_MS, buildOverlayItems } from "./liveOverlayMath";

/**
 * Draws the vehicle and plate boxes over the camera preview.
 *
 * Deliberately a separate memoised component rather than part of the camera
 * section: the preview has no state of its own, so re-rendering it 15 times a
 * second to move boxes would restart the native view for nothing. The boxes
 * live here, in an absolutely positioned layer that never touches the camera.
 *
 * Detection is intermittent, so a box that is not refreshed fades away instead
 * of sticking to a car that has already driven off.
 */
// Shared sentinel so "no detections" has one identity across renders.
const EMPTY = [];

function LiveDetectionOverlay({ message, preview, mode = "cover" }) {
  const [items, setItems] = useState(null);
  const opacity = useRef(new Animated.Value(1)).current;
  const fadeTimer = useRef(null);
  const fadeAnim = useRef(null);

  // buildOverlayItems allocates a fresh array, so it has to be memoised or the
  // effect below would re-run on every render and never settle.
  const projected = useMemo(
    () => buildOverlayItems(message, preview, mode),
    [message, preview.width, preview.height, mode]
  );
  // A stable identity for "no new detections" so an empty frame does not
  // retrigger the fade on every parent render.
  const empty = useMemo(() => (projected.length === 0 ? EMPTY : projected), [projected]);

  useEffect(() => {
    if (fadeTimer.current) clearTimeout(fadeTimer.current);
    if (fadeAnim.current) fadeAnim.current.stop();

    if (!empty) {
      // Nothing detected. Let whatever is on screen fade out.
      fadeAnim.current = Animated.timing(opacity, {
        toValue: 0,
        duration: 200,
        useNativeDriver: true
      });
      fadeAnim.current.start(({ finished }) => {
        if (finished) setItems(null);
      });
      return undefined;
    }

    opacity.setValue(1);
    setItems(empty);
    fadeTimer.current = setTimeout(() => {
      fadeAnim.current = Animated.timing(opacity, {
        toValue: 0,
        duration: 200,
        useNativeDriver: true
      });
      fadeAnim.current.start(({ finished }) => {
        if (finished) setItems(null);
      });
    }, BOX_FADE_MS);
    return undefined;
  }, [empty, opacity]);

  useEffect(
    () => () => {
      if (fadeTimer.current) clearTimeout(fadeTimer.current);
      if (fadeAnim.current) fadeAnim.current.stop();
    },
    []
  );

  if (!items) return null;

  return (
    <Animated.View pointerEvents="none" style={[StyleSheet.absoluteFill, { opacity }]}>
      {items.map((it) => (
        <View
          key={it.key}
          pointerEvents="none"
          style={[
            styles.box,
            {
              left: it.rect.left,
              top: it.rect.top,
              width: it.rect.width,
              height: it.rect.height,
              borderColor: it.color,
              borderWidth: it.borderWidth
            }
          ]}
        >
          {it.label ? (
            <View style={[styles.tag, { backgroundColor: it.color }]}>
              <Text numberOfLines={1} style={styles.tagText}>
                {it.label}
              </Text>
            </View>
          ) : null}
        </View>
      ))}
    </Animated.View>
  );
}

const styles = StyleSheet.create({
  box: {
    position: "absolute",
    borderRadius: 3
  },
  tag: {
    alignSelf: "flex-start",
    paddingHorizontal: 4,
    paddingVertical: 1,
    borderRadius: 3,
    marginTop: -13
  },
  tagText: {
    color: "#0B0F19",
    fontSize: 9,
    fontWeight: "700"
  }
});

export default memo(LiveDetectionOverlay);
