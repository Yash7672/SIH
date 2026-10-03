import { useEffect, useMemo } from "react";
import { useMap } from "react-leaflet";
import L from "leaflet";
// Side-effect import: this is what installs `L.heatLayer`. There is no
// react-leaflet wrapper for it, and adding one would mean a new dependency.
import "leaflet.heat";

export interface HeatPoint {
  lat: number;
  lng: number;
  /** Weight. Normalised to 0..1 before it reaches leaflet.heat. */
  w: number;
}

interface Props {
  points: HeatPoint[];
  /** Pixel radius of influence per point, in metres. */
  radius?: number;
  blur?: number;
  minOpacity?: number;
  /** Cap used to normalise weights; defaults to the data's own maximum. */
  max?: number;
  gradient?: Record<number, string>;
}

const DEFAULT_GRADIENT: Record<number, string> = {
  0.2: "#2F4BD8",
  0.4: "#22C55E",
  0.6: "#F59E0B",
  0.8: "#F97316",
  1.0: "#DC2626",
};

/**
 * leaflet.heat as a react-leaflet child.
 *
 * The layer is created once and then only re-pointed at new data. Rebuilding it
 * on every render would restart the canvas paint on each poll, which is exactly
 * the stutter a heatmap must not have.
 *
 * Weights are normalised here rather than server-side because the server's
 * `max` is a value from the whole window while the visible viewport may hold
 * only the quietest tenth of the data - normalising on the server would paint
 * the visible area uniformly pale.
 */
export default function HeatLayer({
  points,
  radius = 26,
  blur = 20,
  minOpacity = 0.25,
  max,
  gradient = DEFAULT_GRADIENT,
}: Props) {
  const map = useMap();

  const latlngs = useMemo<L.HeatLatLngTuple[]>(() => {
    if (points.length === 0) return [];
    const peak = max && max > 0 ? max : points.reduce((m, p) => Math.max(m, p.w), 0);
    if (!(peak > 0)) return [];
    return points.map((p) => [p.lat, p.lng, Math.max(0.01, Math.min(1, p.w / peak))]);
  }, [points, max]);

  useEffect(() => {
    if (latlngs.length === 0) return undefined;
    const layer = L.heatLayer(latlngs, {
      radius,
      blur,
      minOpacity,
      maxZoom: 17,
      // Keep the canvas above the tile pane but below markers and popups.
      pane: "overlayPane",
      gradient,
    });
    layer.addTo(map);
    return () => {
      map.removeLayer(layer);
    };
    // `gradient` and the scalar options are stable for the page's lifetime;
    // only new data should rebuild the layer.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [map, latlngs]);

  return null;
}