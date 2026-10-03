import type { HeatCell, HeatLayerName } from "../../services/api";

interface Props {
  cells: HeatCell[];
  layer: HeatLayerName;
  gradient: Record<number, string>;
  /** What one unit of weight means, for the legend's screen-reader label. */
  legendLabel: string;
  summary: string | null;
}

function legendBackground(gradient: Record<number, string>) {
  // Keys are 0..1 intensity stops; leaflet.heat paints them low-to-high left to right.
  return `linear-gradient(to right, ${Object.values(gradient).join(", ")})`;
}

/** Colour scale strip under the map. */
export function HeatLegend({ gradient, legendLabel, summary }: Omit<Props, "cells" | "layer">) {
  return (
    <div className="mt-3 flex items-center gap-3">
      <span className="text-xs text-surface-muted">Quiet</span>
      <div
        className="h-2 flex-1 rounded-full"
        style={{ background: legendBackground(gradient) }}
        role="img"
        aria-label={`Colour scale from quiet to busy, in ${legendLabel}`}
      />
      <span className="text-xs text-surface-muted">Busy</span>
      {summary ? <span className="text-xs text-surface-muted">· {summary}</span> : null}
    </div>
  );
}

/**
 * Top cells as a table.
 *
 * The map alone cannot answer "which cell exactly, and how many frames is that
 * based on" - an officer chasing a specific road needs the coordinates, and a
 * single frame with two cars should look different from a junction.
 */
export function CellTable({ cells, layer }: { cells: HeatCell[]; layer: HeatLayerName }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead className="text-xs uppercase tracking-wide text-surface-muted">
          <tr>
            <th className="px-3 py-2 font-medium">Cell</th>
            <th className="px-3 py-2 font-medium">Weight</th>
            <th className="px-3 py-2 font-medium">{layer === "traffic" ? "Frames" : "Sightings"}</th>
          </tr>
        </thead>
        <tbody>
          {cells.slice(0, 8).map((c) => (
            <tr key={`${c.lat},${c.lng}`} className="border-t border-surface-border">
              <td className="px-3 py-2 font-mono text-xs text-surface-text">
                {c.lat.toFixed(3)}, {c.lng.toFixed(3)}
              </td>
              <td className="px-3 py-2 text-surface-text">{c.w.toFixed(2)}</td>
              <td className="px-3 py-2 text-surface-muted">{c.n}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}