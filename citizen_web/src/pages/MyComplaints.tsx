import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { FilePlus2, FilterX, Inbox, Search } from "lucide-react";
import { api, Complaint } from "../services/api";
import { Button } from "../components/ui/Button";
import { Card } from "../components/ui/Card";
import { EmptyState, ErrorState, PageSkeleton } from "../components/ui/Feedback";
import { PlateBadge, StatusChip } from "../components/ui/StatusChip";
import { PageHeader } from "../components/ui/Overlay";
import { Table, TBody, TD, TH, THead, TR } from "../components/ui/Table";

const TABS = [
  { value: "ALL", label: "All" },
  { value: "PENDING", label: "Pending" },
  { value: "UNDER_REVIEW", label: "In review" },
  { value: "VERIFIED", label: "Verified" },
  { value: "HOTLISTED", label: "Hotlisted" },
  { value: "REJECTED", label: "Rejected" },
  { value: "CLOSED", label: "Closed" },
];

/** Debounce a fast-changing value (the search box) so filtering stays cheap. */
function useDebounced<T>(value: T, delay = 300) {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), delay);
    return () => clearTimeout(t);
  }, [value, delay]);
  return debounced;
}

export default function MyComplaints() {
  const [rows, setRows] = useState<Complaint[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState("ALL");
  const [query, setQuery] = useState("");
  const debouncedQuery = useDebounced(query);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    // AbortController so a filter change mid-flight cannot land stale rows.
    const controller = new AbortController();
    api
      .get<Complaint[]>("/complaints/mine", { signal: controller.signal })
      .then((r) => setRows(r.data))
      .catch((e) => {
        if (e.code === "ERR_CANCELED") return;
        setError(e.response?.data?.detail || "Could not load your complaints");
      })
      .finally(() => setLoading(false));
    return () => controller.abort();
  }, []);

  useEffect(load, [load]);

  // Tab counts, recomputed only when the fetched rows change.
  const counts = useMemo(() => {
    const next: Record<string, number> = { ALL: rows.length };
    for (const row of rows) next[row.status] = (next[row.status] || 0) + 1;
    return next;
  }, [rows]);

  const visible = useMemo(() => {
    const q = debouncedQuery.trim().toUpperCase();
    return rows.filter((row) => {
      if (status !== "ALL" && row.status !== status) return false;
      if (!q) return true;
      return (
        row.plate.toUpperCase().includes(q) ||
        String(row.complaint_type).toUpperCase().includes(q) ||
        (row.description || "").toUpperCase().includes(q)
      );
    });
  }, [rows, status, debouncedQuery]);

  if (loading) return <PageSkeleton rows={4} />;

  if (error) {
    return (
      <ErrorState title="Could not load your complaints" description={error} onRetry={load} />
    );
  }

  return (
    <div className="space-y-6">
      <PageHeader
        title="My complaints"
        subtitle="Every vehicle you have reported, and where it is in the verification process."
        action={
          <Link to="/complaints/new">
            <Button icon={<FilePlus2 className="h-4 w-4" aria-hidden />}>Report a vehicle</Button>
          </Link>
        }
      />

      <div className="space-y-4">
        <div className="relative max-w-sm">
          <Search
            className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-surface-muted"
            aria-hidden
          />
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search plate or type"
            aria-label="Search complaints"
            className="h-10 w-full rounded-lg border border-surface-border bg-surface-card pl-9 pr-3 text-sm text-surface-text placeholder:text-surface-subtle focus:border-primary-600 focus:outline-none focus:ring-2 focus:ring-primary-600/25"
          />
        </div>

        <div className="scrollbar-thin -mx-1 flex gap-2 overflow-x-auto px-1 pb-1">
          {TABS.map((tab) => {
            const active = status === tab.value;
            const count = counts[tab.value] || 0;
            return (
              <button
                key={tab.value}
                type="button"
                onClick={() => setStatus(tab.value)}
                aria-pressed={active}
                className={[
                  "inline-flex items-center gap-1.5 whitespace-nowrap rounded-full px-3 py-1.5 text-xs font-medium transition-colors",
                  active
                    ? "bg-solid text-white"
                    : "border border-surface-border bg-surface-card text-surface-muted hover:border-primary-300 hover:text-surface-text",
                ].join(" ")}
              >
                {tab.label}
                <span className={active ? "text-white/75" : "text-surface-subtle"}>{count}</span>
              </button>
            );
          })}
        </div>
      </div>

      {visible.length === 0 ? (
        <Card padding="none">
          <EmptyState
            icon={<Inbox className="h-6 w-6" aria-hidden />}
            title={rows.length === 0 ? "No complaints yet" : "Nothing matches those filters"}
            description={
              rows.length === 0
                ? "Report a stolen or missing vehicle and police will verify it before it enters the hotlist."
                : "Try a different status tab, or clear the search box."
            }
            action={
              rows.length === 0 ? (
                <Link
                  to="/complaints/new"
                  className="inline-flex h-10 items-center rounded-lg bg-solid px-4 text-sm font-medium text-white shadow-sm transition-colors hover:bg-solid-hover"
                >
                  Report a vehicle
                </Link>
              ) : (
                <Button
                  variant="secondary"
                  onClick={() => {
                    setStatus("ALL");
                    setQuery("");
                  }}
                  icon={<FilterX className="h-4 w-4" aria-hidden />}
                >
                  Clear filters
                </Button>
              )
            }
          />
        </Card>
      ) : (
        <Table>
          <THead>
            <TH>Plate</TH>
            <TH>Type</TH>
            <TH>Status</TH>
            <TH className="hidden sm:table-cell">Submitted</TH>
            <TH className="text-right">Action</TH>
          </THead>
          <TBody>
            {visible.map((row) => (
              <TR key={row.id}>
                <TD>
                  <PlateBadge plate={row.plate} size="sm" />
                </TD>
                <TD className="capitalize">{String(row.complaint_type).replace(/_/g, " ")}</TD>
                <TD>
                  <StatusChip status={row.status} />
                </TD>
                <TD className="hidden whitespace-nowrap text-sm text-surface-muted sm:table-cell">
                  {new Date(row.created_at).toLocaleDateString()}
                </TD>
                <TD className="text-right">
                  <Link
                    to={`/complaints/${row.id}`}
                    className="text-sm font-medium text-primary-600 hover:text-primary-700"
                  >
                    View
                  </Link>
                </TD>
              </TR>
            ))}
          </TBody>
        </Table>
      )}
    </div>
  );
}
