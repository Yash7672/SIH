/**
 * Types for leaflet.heat 0.2.0, which ships no .d.ts.
 *
 * The package augments the `leaflet` module object rather than exporting
 * anything, so this declares the `heatLayer` factory it adds. Importing the
 * package for its side effect is what installs it:
 *
 *   import "leaflet.heat";
 *   L.heatLayer(points, options);
 */
import "leaflet";

declare module "leaflet" {
  /** A heat point is [latitude, longitude, intensity] with intensity in 0..1. */
  type HeatLatLngTuple = [number, number, number];

  interface HeatMapOptions {
    /** 1 (lowest) .. max (highest) intensity value present in the data. */
    minOpacity?: number;
    maxZoom?: number;
    max?: number;
    /** Radius in metres of influence of each point. */
    radius?: number;
    /** Pixel blur applied to each point. */
    blur?: number;
    /** Gradient as {value 0..1: css colour}. */
    gradient?: Record<number, string>;
  }

  interface HeatLayerOptions extends HeatMapOptions {
    pane?: string;
    attribution?: string;
  }

  /** Factory installed on `L` by leaflet.heat. */
  function heatLayer(
    latlngs: HeatLatLngTuple[],
    options?: HeatLayerOptions
  ): Layer & { setLatLngs(latlngs: HeatLatLngTuple[]): void; redraw(): void };
}

declare module "leaflet.heat" {
  const plugin: unknown;
  export default plugin;
}