import { useEffect, useRef, useState } from "react";

function prefersReducedMotion() {
  return (
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

/**
 * Animates a numeric statistic from its previous value up to the new one.
 *
 * Only counts the card actually owns should go through here: a value that is
 * already formatted ("92.4", "—") is passed straight to the DOM by the caller,
 * so nothing has to stringify broadly here. The first paint starts at 0, so a
 * card that mounts with its number already in hand still animates, and a change
 * arriving later (a live alert count) resumes from wherever it stopped.
 *
 * `prefers-reduced-motion` collapses the whole thing to the final value.
 */
export function useCountUp(target: number | null, durationMs = 700): number | null {
  const [current, setCurrent] = useState<number | null>(target === null ? null : 0);
  // The value the next animation starts from. Kept in a ref so an interrupted
  // count resumes where it stopped instead of jumping back to zero.
  const fromRef = useRef(0);

  useEffect(() => {
    if (target === null) {
      fromRef.current = 0;
      setCurrent(null);
      return;
    }
    if (prefersReducedMotion()) {
      fromRef.current = target;
      setCurrent(target);
      return;
    }
    const from = fromRef.current;
    if (from === target) {
      setCurrent(target);
      return;
    }
    let raf = 0;
    const start = performance.now();
    const tick = (now: number) => {
      const t = Math.min(1, (now - start) / durationMs);
      // easeOutCubic: fast arrival, gentle settle.
      const eased = 1 - Math.pow(1 - t, 3);
      const next = Math.round(from + (target - from) * eased);
      fromRef.current = next;
      setCurrent(next);
      if (t < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [target, durationMs]);

  return current;
}

export default useCountUp;
