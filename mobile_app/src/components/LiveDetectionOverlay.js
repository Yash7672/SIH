import React, { memo, useEffect, useMemo, useRef } from "react";
import { Animated, StyleSheet, Text, View } from "react-native";
import { FADE_MS, MOVE_MS, selectItems } from "./liveOverlayMath";
import { liveOverlayStore, useOverlayVersion } from "./liveOverlayStore";

/**
 * Draws the vehicle and plate boxes over the camera preview.
 *
 * Deliberately a separate component reading an external store rather than props:
 * the preview has no state of its own, so re-rendering it eight times a second
 * to move a rectangle would restart the native view for nothing. The boxes live
 * in an absolutely positioned layer that never touches the camera.
 *
 * Movement is a *translate*, never a scale and never an animated width/height.
 * The native driver only accepts transform/opacity, and a scaled 3 px border
 * turns into a visibly different border - which is exactly the kind of detail
 * that makes a live view look fake. So: position slides on the UI thread, size
 * changes with the layout in the same commit as the message.
 *
 * A track that is seen for the first time appears at once, without a slide -
 * there is no previous position to slide from, and a fade-in on a car arriving
 * at the edge of the frame reads as lag.
 */

/** How often expiry is swept while boxes are on screen. */
const TICK_MS = 100;

function makeAnim() {
  return {
    x: new Animated.Value(0),
    y: new Animated.Value(0),
    o: new Animated.Value(1),
    placed: false,
    move: null,
    fade: null,
    pulse: null,
    pulsing: false,
    fading: false,
  };
}

function LiveDetectionOverlay({ preview, mode = "cover" }) {
  const version = useOverlayVersion();
  const anims = useRef(new Map());
  const prevPreview = useRef(null);

  // The Animated values live in a ref keyed by track id. Created lazily during
  // render (the usual React Native pattern for a cache of animation objects) so
  // the first paint of a brand new track already has values to drive.
  const getAnim = (key) => {
    let a = anims.current.get(key);
    if (!a) {
      a = makeAnim();
      anims.current.set(key, a);
    }
    return a;
  };

  const items = useMemo(() => {
    const projected = selectItems(liveOverlayStore.state, preview, mode, Date.now());
    return projected.map((it) => ({ ...it, anim: getAnim(it.key) }));
    // `version` is the store's change signal: the state object itself is
    // mutated in place, so it is not a usable dependency.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [version, preview.width, preview.height, mode]);

  const itemsRef = useRef(items);
  itemsRef.current = items;

  useEffect(() => {
    const now = Date.now();
    const live = new Set();

    for (const it of itemsRef.current) {
      live.add(it.key);
      const a = it.anim;
      // A track seen for the first time: put it where it is, no slide. There is
      // no previous position to slide from, and a fade-in on a car arriving at
      // the edge of frame reads as lag rather than as life.
      if (!a.placed) {
        a.placed = true;
        a.x.setValue(it.rect.left);
        a.y.setValue(it.rect.top);
        a.o.setValue(1);
        continue;
      }
      if (a.move) a.move.stop();

      if (it.fading) {
        if (!a.fading) {
          a.fading = true;
          if (a.pulse) a.pulse.stop();
          a.pulsing = false;
          a.fade = Animated.timing(a.o, { toValue: 0, duration: FADE_MS, useNativeDriver: true });
          a.fade.start();
        }
        continue;
      }
      if (a.fading) {
        a.fading = false;
        if (a.fade) a.fade.stop();
      }

      if (it.kind === "plate" && it.stolen) {
        // A hot-list match has to be impossible to miss outdoors, so the plate
        // box pulses until the track expires. Opacity only, for the same reason
        // the movement uses a translate.
        if (!a.pulsing) {
          a.pulsing = true;
          a.pulse = Animated.loop(
            Animated.sequence([
              Animated.timing(a.o, { toValue: 0.3, duration: 400, useNativeDriver: true }),
              Animated.timing(a.o, { toValue: 1, duration: 400, useNativeDriver: true }),
            ])
          );
          a.pulse.start();
        }
        a.x.setValue(it.rect.left);
        a.y.setValue(it.rect.top);
        continue;
      }
      if (a.pulsing) {
        a.pulsing = false;
        if (a.pulse) a.pulse.stop();
      }

      a.o.setValue(1);
      a.move = Animated.parallel([
        Animated.timing(a.x, { toValue: it.rect.left, duration: MOVE_MS, useNativeDriver: true }),
        Animated.timing(a.y, { toValue: it.rect.top, duration: MOVE_MS, useNativeDriver: true }),
      ]);
      a.move.start();
    }

    // Retire tracks the store has dropped, so the map cannot grow without bound
    // over a long shift.
    anims.current.forEach((a, key) => {
      if (live.has(key)) return;
      if (a.move) a.move.stop();
      if (a.pulse) a.pulse.stop();
      if (a.fade) a.fade.stop();
      anims.current.delete(key);
    });

    return () => {
      anims.current.forEach((a) => {
        if (a.move) a.move.stop();
        if (a.pulse) a.pulse.stop();
      });
    };
  }, [version, preview.width, preview.height, mode]);

  // Expiry: a box that stops being reported has to disappear, but only when the
  // messages actually stop. Sweeping on a timer while boxes are visible keeps a
  // dropped connection from leaving the last frame's boxes frozen on screen.
  const hasItems = items.length > 0;
  useEffect(() => {
    if (!hasItems) return undefined;
    const id = setInterval(() => liveOverlayStore.tick(Date.now()), TICK_MS);
    return () => clearInterval(id);
  }, [hasItems]);

  // A rotation or a fold changes the preview size and therefore every position;
  // snapping is better than sliding every box across the screen.
  useEffect(() => {
    if (!prevPreview.current) {
      prevPreview.current = { w: preview.width, h: preview.height };
      return;
    }
    if (prevPreview.current.w === preview.width && prevPreview.current.h === preview.height) return;
    prevPreview.current = { w: preview.width, h: preview.height };
    anims.current.forEach((a) => {
      a.placed = false;
      if (a.move) a.move.stop();
      if (a.pulse) a.pulse.stop();
    });
  }, [preview.width, preview.height]);

  if (!preview.width || !preview.height) return null;

  return (
    <View pointerEvents="none" style={StyleSheet.absoluteFill}>
      {items.map((it) => (
        <BoxLayer key={it.key} item={it} />
      ))}
    </View>
  );
}

/**
 * One rectangle. Re-rendered only when it actually moves: rounding the
 * geometry means a sub-pixel jitter in the detector's output does not dirty
 * twenty views on every message.
 */
const BoxLayer = memo(
  function BoxLayer({ item }) {
    const { rect, anim } = item;
    const w = Math.round(rect.width);
    const h = Math.round(rect.height);
    return (
      <Animated.View
        pointerEvents="none"
        style={[
          styles.box,
          {
            width: w,
            height: h,
            borderColor: item.color,
            borderWidth: item.borderWidth,
            opacity: anim.o,
            transform: [{ translateX: anim.x }, { translateY: anim.y }],
          },
        ]}
      >
        <View
          style={[
            styles.chip,
            item.kind === "vehicle"
              ? { backgroundColor: VEHICLE_CHIP, top: -CHIP_OFFSET }
              : { backgroundColor: PLATE_CHIP, top: item.chipBelow ? h + CHIP_OFFSET : -CHIP_OFFSET },
          ]}
        >
          <Text
            numberOfLines={1}
            style={[styles.chipText, item.kind === "plate" ? styles.plateChipText : null]}
          >
            {item.chip}
          </Text>
        </View>
      </Animated.View>
    );
  },
  (prev, next) => {
    const a = prev.item;
    const b = next.item;
    return (
      a.key === b.key &&
      a.color === b.color &&
      a.borderWidth === b.borderWidth &&
      a.chip === b.chip &&
      a.chipBelow === b.chipBelow &&
      a.stolen === b.stolen &&
      Math.round(a.rect.left) === Math.round(b.rect.left) &&
      Math.round(a.rect.top) === Math.round(b.rect.top) &&
      Math.round(a.rect.width) === Math.round(b.rect.width) &&
      Math.round(a.rect.height) === Math.round(b.rect.height)
    );
  }
);

const VEHICLE_CHIP = "#22C55E";
const PLATE_CHIP = "#FF1F1F";
const CHIP_OFFSET = 15;

const styles = StyleSheet.create({
  box: {
    position: "absolute",
    left: 0,
    top: 0,
    // The chip sits outside the rectangle, so the border must not clip it.
    overflow: "visible",
  },
  chip: {
    position: "absolute",
    paddingHorizontal: 4,
    paddingVertical: 1,
    borderRadius: 3,
    minWidth: 22,
    alignItems: "center",
  },
  chipText: {
    color: "#0B0F19",
    fontSize: 9,
    fontWeight: "800",
    letterSpacing: 0.3,
  },
  plateChipText: {
    color: "#FFFFFF",
  },
});

export default memo(LiveDetectionOverlay);