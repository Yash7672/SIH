import type { HeatLayerName, HeatVehicleClass } from "../../services/api";

/**
 * The window list. The server caps a window at 7 days, so nothing longer is
 * offered here rather than being offered and then rejected with a 422.
 */
const WINDOWS = [
  { label: "15 min", minutes: 15 },
  { label: "1 hour", minutes: 60 },
  { label: "6 hours", minutes: 360 },
  { label: "24 hours", minutes: 1440 },
  { label: "7 days", minutes: 10080 },
] as const;

const CLASSES: Array<{ label: string; value: HeatVehicleClass | "" }> = [
  { label: "All vehicles", value: "" },
  { label: "Two-wheelers", value: "two_wheeler" },
  { label: "Cars", value: "car" },
  { label: "Buses", value: "bus" },
  { label: "Trucks", value: "truck" },
];

const GROUPS = [
  {
    label: "Layer",
    options: [
      { label: "Traffic", value: "traffic" },
      { label: "Stolen sightings", value: "stolen" },
    ] as Array<{ label: string; value: HeatLayerName }>,
  },
  {
    label: "Window",
    options: WINDOWS.map((w) => ({ label: w.label, value: String(w.minutes) })),
  },
];

const PICKED =
  "rounded-lg border px-2.5 py-1.5 text-xs font-medium transition";
const PICKED_ON = "border-primary-600 bg-primary-600/10 text-surface-text";
const PICKED_OFF = "border-surface-border text-surface-muted hover:bg-surface-raised";

interface Props {
  layer: HeatLayerName;
  onLayer: (next: HeatLayerName) => void;
  minutes: number;
  onMinutes: (next: number) => void;
  vehicleClass: HeatVehicleClass | "";
  onVehicleClass: (next: HeatVehicleClass | "") => void;
}

/**
 * Layer / window / vehicle-type pickers.
 *
 * These sit above the map rather than floating over it: a heat canvas under a
 * translucent panel is unreadable, and floating over it covers the very city the
 * officer is trying to read.
 */
export default function HeatFilters({
  layer,
  onLayer,
  minutes,
  onMinutes,
  vehicleClass,
  onVehicleClass,
}: Props) {
  const selected = [layer, String(minutes)];

  return (
    <div className="mb-3 flex flex-wrap items-end gap-4">
      {GROUPS.map((group, i) => (
        <fieldset key={group.label}>
          <legend className="mb-1 text-xs font-medium uppercase tracking-wide text-surface-muted">
            {group.label}
          </legend>
          <div className="flex flex-wrap gap-1">
            {group.options.map((opt) => {
              const on = selected[i] === opt.value;
              return (
                <button
                  key={opt.value}
                  type="button"
                  aria-pressed={on}
                  onClick={() =>
                    i === 0
                      ? onLayer(opt.value as HeatLayerName)
                      : onMinutes(Number(opt.value))
                  }
                  className={`${PICKED} ${i === 0 ? "px-3 text-sm" : ""} ${
                    on ? PICKED_ON : PICKED_OFF
                  }`}
                >
                  {opt.label}
                </button>
              );
            })}
          </div>
        </fieldset>
      ))}

      {/* Only meaningful for traffic: the stolen layer has no class split. */}
      {layer === "traffic" ? (
        <label className="block">
          <span className="mb-1 block text-xs font-medium uppercase tracking-wide text-surface-muted">
            Vehicle type
          </span>
          <select
            value={vehicleClass}
            onChange={(e) => onVehicleClass(e.target.value as HeatVehicleClass | "")}
            className="rounded-lg border border-surface-border bg-surface-card px-3 py-1.5 text-sm text-surface-text"
          >
            {CLASSES.map((c) => (
              <option key={c.label} value={c.value}>
                {c.label}
              </option>
            ))}
          </select>
        </label>
      ) : null}
    </div>
  );
}