import { useCallback, useMemo } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { useTheme } from "../../hooks/useTheme";

export interface HourBucket {
  hour: string;
  count: number;
}

/**
 * Chart colours are read from the same CSS variables as the rest of the app so
 * the plots follow the theme. Recharts draws to SVG attributes, which accept
 * `var(--x)` for stroke/fill, but the tooltip uses `contentStyle` (real DOM), so
 * the resolved values are needed there.
 */
function readVar(name: string, fallback: string) {
  if (typeof window === "undefined") return fallback;
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
}

export default function DetectionsChart({ data }: { data: HourBucket[] }) {
  const { isDark, ready } = useTheme();

  // Recharts needs a referentially stable array to avoid redrawing on every
  // parent render, so the payload is memoised on its contents.
  const payload = useMemo(() => data, [data]);

  // The tooltip needs real colour values, so it is derived once per theme
  // rather than on every pointer move.
  const tooltipStyle = useMemo(
    () => ({
      background: readVar("--surface-card", isDark ? "#111A2E" : "#FFFFFF"),
      border: `1px solid ${isDark ? "#263552" : "#E2E8F0"}`,
      borderRadius: 8,
      fontSize: 12,
      color: readVar("--surface-text", isDark ? "#E6EDF7" : "#0F172A"),
      boxShadow: isDark
        ? "0 10px 15px -3px rgb(0 0 0 / 0.6)"
        : "0 10px 15px -3px rgb(15 23 42 / 0.1)",
    }),
    [isDark]
  );

  const labelStyle = useCallback(
    () => ({ color: readVar("--surface-text", "#0F172A"), fontWeight: 600 as const }),
    []
  );

  // Until the stored preference has been read, draw nothing rather than flash
  // the wrong palette.
  if (!ready) return <div className="h-72 p-4" />;

  return (
    <div className="h-72 p-4">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={payload} margin={{ top: 8, right: 12, left: -22, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="var(--chart-grid)" vertical={false} />
          <XAxis
            dataKey="hour"
            tick={{ fill: "var(--chart-axis)", fontSize: 11 }}
            interval="preserveStartEnd"
            tickLine={false}
            axisLine={{ stroke: "var(--chart-grid)" }}
          />
          <YAxis
            allowDecimals={false}
            tick={{ fill: "var(--chart-axis)", fontSize: 11 }}
            tickLine={false}
            axisLine={false}
          />
          <Tooltip
            cursor={{ fill: "var(--row-hover)" }}
            contentStyle={tooltipStyle}
            labelStyle={labelStyle()}
          />
          <Bar
            dataKey="count"
            name="Detections"
            fill="var(--solid)"
            radius={[6, 6, 0, 0]}
            maxBarSize={36}
          />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
