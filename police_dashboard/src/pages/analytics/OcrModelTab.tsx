import { useCallback, useEffect, useMemo, useState } from "react";
import type { CSSProperties } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  type TooltipProps,
} from "recharts";
import { AlertTriangle, CheckCircle2, XCircle } from "lucide-react";
import { fetchOcrReport, type OcrNoReport, type OcrReport, type OcrSetMetrics } from "../../services/api";
import { Card, CardHeader } from "../../components/ui/Card";
import { StatCard } from "../../components/ui/StatCard";
import { EmptyState, ErrorState, Skeleton } from "../../components/ui/Feedback";
import { Button } from "../../components/ui/Button";
import { useTheme } from "../../hooks/useTheme";

/**
 * "OCR Model" tab: measured accuracy from `python scripts/eval_ocr.py`,
 * served read-only by GET /analytics/ocr. Nothing here changes how plates are
 * read; it only displays the report file. Lazy-loaded from Analytics so the
 * charts do not grow the entry bundle.
 */

const PILL_ACTIVE = "rounded-md bg-surface-card px-3 py-1.5 text-xs font-medium text-surface-text shadow";
const PILL_IDLE = "rounded-md px-3 py-1.5 text-xs font-medium text-surface-muted hover:text-surface-text";

/** The "missing or extra" bucket the metric maths folds deletions/insertions into. */
const MISSING_LABEL = "\u2205";

function pct(value: number | null | undefined, digits = 1): string {
  return value == null ? "—" : `${(value * 100).toFixed(digits)}%`;
}

function ms(value: number | null | undefined): string {
  return value == null ? "—" : `${Math.round(value)} ms`;
}

/** Chart colours come from the same CSS variables as the rest of the app. */
function readVar(name: string, fallback: string) {
  if (typeof window === "undefined") return fallback;
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
}

function useTooltipStyle() {
  const { isDark } = useTheme();
  return useMemo(
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
}

/**
 * Per-dataset plate accuracy and its complement, the error rate: one green
 * series that must clear 90%, one red series that must stay under 10%. Sets
 * with no samples are dropped - a 0-height bar there would be a lie, not a
 * measurement.
 */
function AccuracyVsError({ sets, target }: { sets: OcrSetMetrics[]; target: OcrReport["target"] }) {
  const { ready } = useTheme();
  const tooltipStyle = useTooltipStyle();
  const data = useMemo(
    () =>
      sets
        .filter((s) => s.n > 0 && s.plate_accuracy != null)
        .map((s) => {
          const accuracy = (s.plate_accuracy as number) * 100;
          return {
            name: s.name,
            accuracy,
            error: Math.max(0, 100 - accuracy),
            n: s.n,
          };
        }),
    [sets]
  );
  const targetPct = target.value * 100;
  const tooltip = useMemo(
    () =>
      ({ active, payload, label }: TooltipProps<number, string>) =>
        !active || !payload?.length ? null : (
          <div style={tooltipStyle} className="px-3 py-2">
            <p className="mb-1 text-xs font-medium capitalize">{label}</p>
            {payload
              .filter((p) => p.dataKey === "accuracy" || p.dataKey === "error")
              .map((p) => (
                <p key={p.dataKey} className="text-xs">
                  <span style={{ color: p.color, fontWeight: 600 }}>
                    {p.dataKey === "accuracy" ? "Accuracy" : "Error rate"}
                  </span>{" "}
                  {p.value == null ? "—" : `${(p.value as number).toFixed(1)}%`}
                </p>
              ))}
            <p className="mt-1 text-[10px] opacity-70">n = {payload[0].payload?.n ?? 0}</p>
          </div>
        ),
    [tooltipStyle]
  );
  if (!ready) return <div className="h-72 p-4" />;
  if (data.length === 0) {
    return (
      <p className="h-72 p-4 text-sm text-surface-muted">No evaluation set had any samples.</p>
    );
  }
  return (
    <div className="h-72 p-4">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 8, right: 12, left: -22, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="var(--chart-grid)" vertical={false} />
          <XAxis
            dataKey="name"
            tick={{ fill: "var(--chart-axis)", fontSize: 11 }}
            tickLine={false}
            axisLine={{ stroke: "var(--chart-grid)" }}
          />
          <YAxis
            domain={[0, 100]}
            unit="%"
            tick={{ fill: "var(--chart-axis)", fontSize: 11 }}
            tickLine={false}
            axisLine={false}
          />
          <Tooltip cursor={{ fill: "var(--row-hover)" }} content={tooltip} />
          <ReferenceLine
            y={targetPct}
            stroke="var(--warning)"
            strokeDasharray="6 4"
            label={{
              value: `${targetPct}% target`,
              position: "insideTopRight",
              fill: "var(--surface-muted)",
              fontSize: 10,
            }}
          />
          <Bar dataKey="accuracy" name="Plate accuracy" fill="var(--success)" radius={[6, 6, 0, 0]} maxBarSize={40} />
          <Bar dataKey="error" name="Error rate" fill="var(--danger)" radius={[6, 6, 0, 0]} maxBarSize={40} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

/** Substitutions vs deletions vs insertions, read straight off the matrix. */
function ErrorComposition({ confusion }: { confusion: OcrReport["confusion"] }) {
  const { ready } = useTheme();
  const tooltipStyle = useTooltipStyle();
  const data = useMemo(() => {
    const missing = confusion.labels.indexOf(MISSING_LABEL);
    let substitutions = 0;
    let deletions = 0;
    let insertions = 0;
    confusion.counts.forEach((row, i) => {
      row.forEach((count, j) => {
        if (!count) return;
        if (i === missing && j === missing) return;
        if (i === missing) insertions += count;
        else if (j === missing) deletions += count;
        else if (i !== j) substitutions += count;
      });
    });
    return [
      { name: "Substitution", count: substitutions, hint: "wrong character read" },
      { name: "Deletion", count: deletions, hint: "character missed" },
      { name: "Insertion", count: insertions, hint: "character invented" },
    ];
  }, [confusion]);
  const total = data.reduce((a, b) => a + b.count, 0);
  if (!ready) return <div className="h-72 p-4" />;
  if (total === 0) {
    return (
      <div className="flex h-72 flex-col items-center justify-center gap-1 p-4 text-center">
        <CheckCircle2 className="h-6 w-6 text-success" aria-hidden />
        <p className="text-sm text-surface-text">No character errors to break down.</p>
        <p className="text-xs text-surface-muted">Every aligned character matched.</p>
      </div>
    );
  }
  return (
    <div className="h-72 p-4">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} layout="vertical" margin={{ top: 8, right: 24, left: 8, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="var(--chart-grid)" horizontal={false} />
          <XAxis
            type="number"
            allowDecimals={false}
            tick={{ fill: "var(--chart-axis)", fontSize: 11 }}
            tickLine={false}
            axisLine={{ stroke: "var(--chart-grid)" }}
          />
          <YAxis
            type="category"
            dataKey="name"
            width={90}
            tick={{ fill: "var(--chart-axis)", fontSize: 11 }}
            tickLine={false}
            axisLine={false}
          />
          <Tooltip
            cursor={{ fill: "var(--row-hover)" }}
            contentStyle={tooltipStyle}
            formatter={(value: number, _name, item) => [
              `${value} (${((value / total) * 100).toFixed(0)}% of errors)`,
              item?.payload?.hint ?? "Errors",
            ]}
          />
          <Bar dataKey="count" fill="var(--danger)" radius={[0, 6, 6, 0]} maxBarSize={36} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

/** Per-character accuracy (recall), worst first, so weak glyphs stand out. */
function WeakestCharacters({ perChar }: { perChar: OcrReport["confusion"]["per_char"] }) {
  const { ready } = useTheme();
  const tooltipStyle = useTooltipStyle();
  const data = useMemo(
    () =>
      perChar
        .filter((c) => c.support > 0)
        .map((c) => ({ char: c.char, accuracy: c.recall * 100, support: c.support }))
        .sort((a, b) => a.accuracy - b.accuracy || b.support - a.support)
        .slice(0, 14),
    [perChar]
  );
  const height = Math.max(240, data.length * 26 + 40);
  if (!ready) return <div className="h-72 p-4" />;
  if (data.length === 0) {
    return <p className="p-4 text-sm text-surface-muted">No character had any labelled support.</p>;
  }
  return (
    <div className="p-4" style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} layout="vertical" margin={{ top: 4, right: 40, left: 4, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="var(--chart-grid)" horizontal={false} />
          <XAxis
            type="number"
            domain={[0, 100]}
            unit="%"
            tick={{ fill: "var(--chart-axis)", fontSize: 11 }}
            tickLine={false}
            axisLine={{ stroke: "var(--chart-grid)" }}
          />
          <YAxis
            type="category"
            dataKey="char"
            width={40}
            tick={{ fill: "var(--chart-axis)", fontSize: 12, fontFamily: "monospace" }}
            tickLine={false}
            axisLine={false}
          />
          <Tooltip
            cursor={{ fill: "var(--row-hover)" }}
            contentStyle={tooltipStyle}
            formatter={(value: number, _name, item) => [
              item?.payload?.support
                ? `${value.toFixed(1)}% on ${item.payload.support} sample${item.payload.support === 1 ? "" : "s"}`
                : "—",
              "Character accuracy",
            ]}
          />
          <ReferenceLine
            x={90}
            stroke="var(--warning)"
            strokeDasharray="6 4"
            label={{ value: "90%", position: "top", fill: "var(--surface-muted)", fontSize: 10 }}
          />
          <Bar dataKey="accuracy" fill="var(--solid)" radius={[0, 6, 6, 0]} maxBarSize={18} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

function ConfidenceHistogram({ histogram }: { histogram: OcrReport["confidence_histogram"] }) {
  const { ready } = useTheme();
  const tooltipStyle = useTooltipStyle();
  const data = useMemo(
    () =>
      histogram.bins.map((bin, i) => ({
        bin,
        correct: histogram.correct[i] ?? 0,
        wrong: histogram.wrong[i] ?? 0,
      })),
    [histogram]
  );
  if (!ready) return <div className="h-72 p-4" />;
  return (
    <div className="h-72 p-4">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 8, right: 12, left: -22, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="var(--chart-grid)" vertical={false} />
          <XAxis
            dataKey="bin"
            tick={{ fill: "var(--chart-axis)", fontSize: 9 }}
            tickLine={false}
            axisLine={{ stroke: "var(--chart-grid)" }}
            interval="preserveStartEnd"
          />
          <YAxis
            allowDecimals={false}
            tick={{ fill: "var(--chart-axis)", fontSize: 11 }}
            tickLine={false}
            axisLine={false}
          />
          <Tooltip cursor={{ fill: "var(--row-hover)" }} contentStyle={tooltipStyle} />
          <Bar dataKey="correct" name="Correct" stackId="a" fill="var(--success)" />
          <Bar dataKey="wrong" name="Wrong or no read" stackId="a" fill="var(--danger)" radius={[6, 6, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

function ConfusionMatrix({ confusion }: { confusion: OcrReport["confusion"] }) {
  const { labels, counts, normalized } = confusion;
  const [mode, setMode] = useState<"counts" | "percent">("counts");
  const [errorsOnly, setErrorsOnly] = useState(true);

  const size = labels.length;
  const rowHasError = useMemo(
    () => labels.map((_, i) => counts[i].reduce((a, b) => a + b, 0) > counts[i][i]),
    [labels, counts]
  );
  const rows = useMemo(
    () =>
      labels
        .map((label, i) => ({ label, i }))
        .filter(({ i }) => (errorsOnly ? rowHasError[i] : true)),
    [labels, rowHasError, errorsOnly]
  );
  const maxOff = useMemo(
    () => Math.max(1, ...counts.flatMap((row, i) => row.filter((_, j) => j !== i))),
    [counts]
  );
  const maxOffPct = useMemo(
    () => Math.max(0.0001, ...normalized.flatMap((row, i) => row.filter((_, j) => j !== i))),
    [normalized]
  );

  const columns = `28px repeat(${size}, minmax(24px, 1fr))`;

  function cellStyle(i: number, j: number): CSSProperties {
    const count = counts[i][j];
    if (!count) return { background: "transparent" };
    const frac =
      i === j
        ? normalized[i][j]
        : mode === "counts"
          ? count / maxOff
          : normalized[i][j] / maxOffPct;
    const alpha = (0.1 + 0.8 * Math.min(1, frac)).toFixed(2);
    // Diagonal (correct) and off-diagonal (error) use different colour ramps.
    return {
      background:
        i === j ? `rgb(var(--success) / ${alpha})` : `rgb(var(--danger) / ${alpha})`,
    };
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex rounded-lg bg-surface-sunken p-0.5" role="group" aria-label="Matrix values">
          <button type="button" className={mode === "counts" ? PILL_ACTIVE : PILL_IDLE} aria-pressed={mode === "counts"} onClick={() => setMode("counts")}>
            Counts
          </button>
          <button type="button" className={mode === "percent" ? PILL_ACTIVE : PILL_IDLE} aria-pressed={mode === "percent"} onClick={() => setMode("percent")}>
            Percent
          </button>
        </div>
        <label className="flex cursor-pointer items-center gap-2 text-xs text-surface-muted">
          <input
            type="checkbox"
            checked={errorsOnly}
            onChange={(e) => setErrorsOnly(e.target.checked)}
            className="h-3.5 w-3.5 rounded border-surface-border"
          />
          Show only characters with errors
        </label>
        <span className="flex items-center gap-3 text-[10px] text-surface-muted">
          <span className="inline-flex items-center gap-1">
            <span className="h-3 w-3 rounded-sm" style={{ background: "rgb(var(--success) / 0.8)" }} aria-hidden />
            correct
          </span>
          <span className="inline-flex items-center gap-1">
            <span className="h-3 w-3 rounded-sm" style={{ background: "rgb(var(--danger) / 0.8)" }} aria-hidden />
            error
          </span>
        </span>
      </div>

      {rows.length === 0 ? (
        <p className="rounded-lg bg-surface-sunken px-4 py-6 text-center text-sm text-surface-muted">
          No character errors in this evaluation set.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <div
            className="min-w-max"
            role="grid"
            aria-label="Character confusion matrix: rows are the true plate characters, columns are what the model read"
          >
            <div role="row" className="grid" style={{ gridTemplateColumns: columns }}>
              <span aria-hidden className="h-6" />
              {labels.map((label) => (
                <span
                  key={`h-${label}`}
                  role="columnheader"
                  className="flex h-6 items-center justify-center font-mono text-[9px] text-surface-muted"
                >
                  {label}
                </span>
              ))}
            </div>
            {rows.map(({ label, i }) => (
              <div role="row" key={label} className="grid" style={{ gridTemplateColumns: columns }}>
                <span
                  role="rowheader"
                  className="flex w-7 items-center justify-center font-mono text-[10px] font-medium text-surface-text"
                >
                  {label}
                </span>
                {labels.map((pred, j) => {
                  const count = counts[i][j];
                  const share = normalized[i][j];
                  const shown =
                    mode === "counts"
                      ? count || ""
                      : share
                        ? `${(share * 100).toFixed(0)}%`
                        : "";
                  const describe = `true ${label === "∅" ? "missing" : label}, read as ${pred === "∅" ? "missing/extra" : pred}: ${count} (${(share * 100).toFixed(1)}% of ${label} rows)`;
                  return (
                    <div
                      key={`${label}-${pred}`}
                      role="gridcell"
                      aria-label={describe}
                      title={describe}
                      style={cellStyle(i, j)}
                      className="m-px flex h-6 items-center justify-center rounded-[3px] font-medium text-surface-text"
                    >
                      <span className="text-[9px]">{shown}</span>
                    </div>
                  );
                })}
              </div>
            ))}
          </div>
        </div>
      )}
      <p className="text-[10px] text-surface-muted">
        37 classes: 0-9, A-Z and ∅ (a character the model missed, or one it invented). Row percentages
        sum to 100% down each true character.
      </p>
    </div>
  );
}

function TopConfusions({ top }: { top: OcrReport["confusion"]["top"] }) {
  const rows = top.slice(0, 10);
  if (rows.length === 0) {
    return (
      <p className="rounded-lg bg-surface-sunken px-4 py-6 text-center text-sm text-surface-muted">
        No character errors in this evaluation set.
      </p>
    );
  }
  return (
    <table className="w-full text-sm">
      <thead>
        <tr className="border-b border-surface-border text-left text-[11px] uppercase tracking-[0.08em] text-surface-muted">
          <th className="py-2 pr-3 font-medium">True</th>
          <th className="py-2 pr-3 font-medium">Read as</th>
          <th className="py-2 pr-3 text-right font-medium">Count</th>
          <th className="py-2 text-right font-medium">Share of that character's errors</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((c) => (
          <tr key={`${c.true}-${c.pred}`} className="border-b border-surface-border/60">
            <td className="py-2 pr-3 font-mono font-semibold text-surface-text">{c.true}</td>
            <td className="py-2 pr-3 font-mono font-semibold text-surface-text">{c.pred}</td>
            <td className="py-2 pr-3 text-right font-medium text-surface-text">{c.count}</td>
            <td className="py-2 text-right text-surface-muted">{(c.share * 100).toFixed(0)}%</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function PerCharacterTable({ perChar }: { perChar: OcrReport["confusion"]["per_char"] }) {
  const [sortDesc, setSortDesc] = useState(true);
  const rows = useMemo(
    () => [...perChar].sort((a, b) => (sortDesc ? b.recall - a.recall : a.recall - b.recall)),
    [perChar, sortDesc]
  );
  return (
    <div className="max-h-96 overflow-y-auto">
      <table className="w-full text-sm">
        <thead className="sticky top-0 bg-surface-card">
          <tr className="border-b border-surface-border text-left text-[11px] uppercase tracking-[0.08em] text-surface-muted">
            <th className="py-2 pr-3 font-medium">Char</th>
            <th className="py-2 pr-3 text-right font-medium">Support</th>
            <th className="py-2 pr-3 text-right font-medium">Precision</th>
            <th className="py-2 text-right font-medium" aria-sort={sortDesc ? "descending" : "ascending"}>
              <button
                type="button"
                className="uppercase tracking-[0.08em] hover:text-surface-text"
                onClick={() => setSortDesc((v) => !v)}
              >
                Recall {sortDesc ? "↓" : "↑"}
              </button>
            </th>
            <th className="py-2 text-right font-medium">F1</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((c) => (
            <tr key={c.char} className="border-b border-surface-border/60">
              <td className="py-1.5 pr-3 font-mono font-semibold text-surface-text">{c.char}</td>
              <td className="py-1.5 pr-3 text-right text-surface-muted">{c.support}</td>
              <td className="py-1.5 pr-3 text-right text-surface-text">{c.precision.toFixed(2)}</td>
              <td className="py-1.5 text-right text-surface-text">{c.support ? c.recall.toFixed(2) : "—"}</td>
              <td className="py-1.5 text-right text-surface-text">{c.support ? c.f1.toFixed(2) : "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function formatIst(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString("en-IN", {
    timeZone: "Asia/Kolkata",
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export default function OcrModelTab() {
  const [report, setReport] = useState<OcrReport | null>(null);
  const [noReport, setNoReport] = useState<OcrNoReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetchOcrReport()
      .then((data) => {
        if (cancelled) return;
        if (data.status === "no_report") {
          setNoReport(data);
          setReport(null);
        } else {
          setReport(data as OcrReport);
          setNoReport(null);
        }
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        const detail =
          (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
        setError(detail || "Could not load the OCR report from the backend.");
      })
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [reload]);

  const refresh = useCallback(() => setReload((v) => v + 1), []);

  if (loading) {
    return (
      <div className="space-y-6" aria-busy="true" aria-label="Loading OCR report">
        <div className="grid grid-cols-2 gap-4 md:grid-cols-3 xl:grid-cols-5">
          <Skeleton className="h-24 rounded-xl" />
          <Skeleton className="h-24 rounded-xl" />
          <Skeleton className="h-24 rounded-xl" />
          <Skeleton className="h-24 rounded-xl" />
          <Skeleton className="h-24 rounded-xl" />
        </div>
        <Skeleton className="h-80 rounded-xl" />
        <Skeleton className="h-80 rounded-xl" />
      </div>
    );
  }

  if (error) {
    return <ErrorState title="OCR report unavailable" description={error} onRetry={refresh} />;
  }

  if (noReport || !report) {
    return (
      <Card>
        <EmptyState
          icon={<XCircle className="h-6 w-6" aria-hidden />}
          title="No OCR evaluation yet"
          description="Run the offline evaluation on the backend machine, then refresh. The tab only reads the report; it never runs the evaluation itself."
          action={
            <code className="rounded bg-surface-sunken px-2 py-1 font-mono text-xs text-surface-text">
              python scripts/eval_ocr.py
            </code>
          }
        />
      </Card>
    );
  }

  const { target, kpis, sets, confusion, confidence_histogram: histogram, metadata } = report;
  const realSet = sets.find((s) => s.name === "real");
  const syntheticSet = sets.find((s) => s.name === "synthetic");
  const meets = target.met_point;
  const proven = target.met_proven;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <span
            className={[
              "inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-sm font-medium",
              meets ? "bg-success/10 text-success" : "bg-danger/10 text-danger",
            ].join(" ")}
            role="status"
          >
            {meets ? <CheckCircle2 className="h-4 w-4" aria-hidden /> : <XCircle className="h-4 w-4" aria-hidden />}
            {meets ? "Meets 90% target" : "Below 90% target"}
          </span>
          <span
            className={[
              "rounded-full px-2.5 py-1 text-xs font-medium",
              proven ? "bg-success/10 text-success" : "bg-surface-sunken text-surface-muted",
            ].join(" ")}
          >
            {proven ? "statistically proven" : "not statistically proven"}
          </span>
          <span className="text-xs text-surface-muted">
            95% CI {pct(target.ci_low)} to {pct(target.ci_high)}
          </span>
          {target.flag ? (
            <span className="rounded-full bg-warning/10 px-2.5 py-1 text-xs font-medium text-warning">
              {target.flag}
            </span>
          ) : null}
        </div>
        <Button
          variant="secondary"
          size="sm"
          onClick={refresh}
          icon={<span aria-hidden>↻</span>}
        >
          Refresh
        </Button>
      </div>

      <div className="grid grid-cols-2 gap-4 md:grid-cols-3 xl:grid-cols-5">
        <StatCard
          label="Plate accuracy"
          value={pct(target.accuracy)}
          hint={`headline: ${target.headline_set}`}
          tone={meets ? "success" : "danger"}
        />
        <StatCard label="Error rate" value={pct(kpis.error_rate)} hint="1 − accuracy" tone="danger" />
        <StatCard
          label="Character accuracy"
          value={pct(kpis.character_accuracy)}
          hint={`CER ${pct(kpis.cer)}`}
        />
        <StatCard
          label="Samples"
          value={`${realSet?.n ?? 0} / ${syntheticSet?.n ?? 0}`}
          hint="real / synthetic"
        />
        <StatCard label="Latency p50" value={ms(kpis.latency_p50_ms)} hint={`p95 ${ms(kpis.latency_p95_ms)}`} tone="neutral" />
      </div>

      {kpis.wrong_confident_count > 0 ? (
        <div className="flex items-start gap-3 rounded-xl border border-warning/40 bg-warning/10 px-4 py-3 text-sm text-surface-text">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warning" aria-hidden />
          <span>
            <b>{kpis.wrong_confident_count}</b> read{kpis.wrong_confident_count === 1 ? " was" : "s were"} wrong but confident
            (confidence ≥ 0.8) — {pct(kpis.wrong_confident_rate)} of the set. These are the dangerous
            errors: the live path trusts them.
          </span>
        </div>
      ) : (
        <div className="flex items-center gap-2 rounded-xl border border-surface-border bg-surface-card px-4 py-3 text-sm text-surface-muted">
          <CheckCircle2 className="h-4 w-4 text-success" aria-hidden />
          0 wrong-but-confident reads (confidence ≥ 0.8) in this evaluation set.
        </div>
      )}

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader
            title="Plate accuracy vs error rate by dataset"
            subtitle={`Exact plate matches (green) and 1 − accuracy (red); dashed line is the ${(target.value * 100).toFixed(0)}% target. Real n=${realSet?.n ?? 0}, synthetic n=${syntheticSet?.n ?? 0}.`}
          />
          <AccuracyVsError sets={sets} target={target} />
        </Card>
        <Card>
          <CardHeader
            title="Confidence histogram"
            subtitle={`Read confidence for correct vs wrong reads. Accuracy at conf ≥ 0.8: ${pct(kpis.accuracy_at_conf_08)} on ${pct(kpis.coverage_at_conf_08)} coverage.`}
          />
          <ConfidenceHistogram histogram={histogram} />
          <p className="px-4 pb-3 text-[10px] text-surface-muted">
            <span className="mr-3 inline-flex items-center gap-1">
              <span className="h-2 w-2 rounded-sm bg-success" aria-hidden /> correct
            </span>
            <span className="inline-flex items-center gap-1">
              <span className="h-2 w-2 rounded-sm bg-danger" aria-hidden /> wrong or no read
            </span>
          </p>
        </Card>
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader
            title="Character errors by type"
            subtitle="Substitutions, deletions and insertions, taken from the Levenshtein alignment."
          />
          <ErrorComposition confusion={confusion} />
        </Card>
        <Card>
          <CardHeader
            title="Weakest characters"
            subtitle="Per-character accuracy (recall), worst first; dashed line is the 90% target."
          />
          <WeakestCharacters perChar={confusion.per_char} />
        </Card>
      </div>

      <Card>
        <CardHeader
          title="Character confusion matrix"
          subtitle="What each true plate character was read as, from the Levenshtein alignment of expected vs read strings."
        />
        <div className="mt-4">
          <ConfusionMatrix confusion={confusion} />
        </div>
      </Card>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader title="Top confusions" subtitle="Most frequent wrong character reads." />
          <div className="mt-2">
            <TopConfusions top={confusion.top} />
          </div>
        </Card>
        <Card>
          <CardHeader
            title="Per-character accuracy"
            subtitle="Precision / recall / F1 per plate character. Click Recall to re-sort."
          />
          <div className="mt-2">
            <PerCharacterTable perChar={confusion.per_char} />
          </div>
        </Card>
      </div>

      <Card>
        <div className="space-y-1 text-xs text-surface-muted">
          <p>
            Generated {formatIst(metadata.generated_at)} · commit{" "}
            <code className="font-mono">{metadata.commit.slice(0, 12)}</code> · dataset {metadata.dataset.real_files} real +{" "}
            {metadata.dataset.synthetic_files} synthetic plates · character verifier{" "}
            {metadata.char_verifier ? "ON" : "OFF"}
            {metadata.dataset.training_manifest_excluded
              ? ` · ${metadata.dataset.training_manifest_excluded} training-manifest files excluded`
              : ""}
          </p>
          <p>
            Re-run on the backend machine:{" "}
            <code className="rounded bg-surface-sunken px-1.5 py-0.5 font-mono">python scripts/eval_ocr.py</code>
          </p>
          {metadata.dataset.skipped && metadata.dataset.skipped.length > 0 ? (
            <details>
              <summary className="cursor-pointer hover:text-surface-text">
                {metadata.dataset.skipped.length} file{metadata.dataset.skipped.length === 1 ? "" : "s"} skipped (no plate in the file name)
              </summary>
              <ul className="mt-1 list-disc pl-5">
                {metadata.dataset.skipped.map((s) => (
                  <li key={s.file}>
                    <code className="font-mono">{s.file}</code> — {s.reason}
                  </li>
                ))}
              </ul>
            </details>
          ) : null}
          {metadata.dataset.char_dataset_note ? (
            <details>
              <summary className="cursor-pointer hover:text-surface-text">Which datasets were used</summary>
              <p className="mt-1 max-w-3xl">{metadata.dataset.char_dataset_note}</p>
            </details>
          ) : null}
        </div>
      </Card>
    </div>
  );
}
