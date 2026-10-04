import { useSyncExternalStore } from "react";
import {
  applyBoxes,
  applyPlateText,
  applyPlates,
  clearState,
  createOverlayState,
  tick,
} from "./liveOverlayMath";

/**
 * A tiny external store for the detection geometry.
 *
 * This exists for one reason: the camera preview must not re-render when a box
 * moves. Pushing the boxes through React state owned by the screen would
 * re-render the whole scanner - CameraView included - eight times a second.
 * The server messages land here instead, the overlay subscribes, and only the
 * overlay's own layer re-renders.
 *
 * It is a module singleton rather than a context: there is exactly one camera.
 */
const state = createOverlayState();
const listeners = new Set();
let version = 0;

function emit() {
  version += 1;
  listeners.forEach((fn) => {
    try {
      fn();
    } catch (e) {
      // One bad subscriber must not stop the others from being told.
    }
  });
}

export const liveOverlayStore = {
  /** The live geometry. Read during render; never mutate it from a component. */
  state,
  subscribe(fn) {
    listeners.add(fn);
    return () => listeners.delete(fn);
  },
  getVersion() {
    return version;
  },
  applyBoxes(message) {
    applyBoxes(state, message, Date.now());
    emit();
  },
  applyPlates(message) {
    applyPlates(state, message, Date.now());
    emit();
  },
  applyPlateText(message) {
    const rec = applyPlateText(state, message, Date.now());
    emit();
    return rec;
  },
  /** Expiry sweep. Returns true when it changed anything. */
  tick(now) {
    const changed = tick(state, now);
    if (changed) emit();
    return changed;
  },
  clear() {
    clearState(state);
    emit();
  },
};

export function useOverlayVersion() {
  return useSyncExternalStore(liveOverlayStore.subscribe, liveOverlayStore.getVersion);
}