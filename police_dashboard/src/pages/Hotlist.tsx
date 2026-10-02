import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Search, ShieldAlert } from "lucide-react";
import { api, HotlistEntry } from "../services/api";
import { Button } from "../components/ui/Button";
import { Card } from "../components/ui/Card";
import { Modal, Toast } from "../components/ui/Overlay";
import { HotlistChip } from "../components/ui/HotlistChip";
import { PlateBadge } from "../components/ui/StatusChip";
import { Pagination } from "../components/ui/Pagination";
import { TD, TH, TBody, THead, TR, Table } from "../components/ui/Table";
import { EmptyState, ErrorState, Skeleton } from "../components/ui/Feedback";
import { PageHeader } from "../components/ui/Overlay";
import { formatCoords, platePath, timeAgo } from "../lib/format";

const PAGE_SIZE = 50;

const FILTERS = [
  { value: "ALL", label: "All" },
  { value: "ACTIVE", label: "Active" },
  { value: "FIR_CONFIRMED", label: "FIR confirmed" },
  { value: "RECOVERED", label: "Recovered" },
  { value: "CLOSED", label: "Closed" },
  { value: "EXPIRED", label: "Expired" },
];

/** Status transitions offered in the row menu. Each needs confirmation. */
const TRANSITIONS: Record<string, Array<{ status: string; label: string; variant: "primary" | "secondary" | "danger" }>> = {
  ACTIVE: [
    { status: "FIR_CONFIRMED", label: "Confirm FIR", variant: "primary" },
    { status: "RECOVERED", label: "Mark recovered", variant: "primary" },
    { status: "CLOSED", label: "Close entry", variant: "secondary" },
  ],
  FIR_CONFIRMED: [
    { status: "RECOVERED", label: "Mark recovered", variant: "primary" },
    { status: "CLOSED", label: "Close entry", variant: "secondary" },
  ],
};

function useDebounced<T>(value: T, delay = 300) {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), delay);
    return () => clearTimeout(t);
  }, [value, delay]);
  return debounced;
}

export default function Hotlist() {
  const [items, setItems] = useState<HotlistEntry[]>([]);
  const [filter, setFilter] = useState("ALL");
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<{ entry: HotlistEntry; status: string; label: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState<{ tone: "success" | "danger"; message: string } | null>(null);
  const debouncedQuery = useDebounced(query);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    const q = filter === "ALL" ? "" : `?status_filter=${filter}`;
    api
      .get<HotlistEntry[]>(`/hotlist${q}`)
      .then((r) => setItems(r.data))
      .catch((e) => {
        const d = e.response?.data?.detail;
        setError(typeof d === "string" ? d : "Could not load the hotlist");
      })
      .finally(() => setLoading(false));
  }, [filter]);

  useEffect(load, [load]);
  useEffect(() => setPage(0), [filter, debouncedQuery]);

  const filtered = useMemo(() => {
    const q = debouncedQuery.trim().toUpperCase();
    if (!q) return items;
    return items.filter(
      (h) =>
        h.plate.toUpperCase().includes(q) ||
        (h.fir_reference || "").toUpperCase().includes(q) ||
        (h.complaint_id || "").toUpperCase().includes(q)
    );
  }, [items, debouncedQuery]);

  const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const rows = useMemo(() => filtered.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE), [filtered, page]);

  async function patch() {
    if (!confirm) return;
    setBusy(true);
    try {
      await api.patch(`/hotlist/${confirm.entry.id}`, { status: confirm.status });
      setToast({ tone: "success", message: `${confirm.entry.plate} → ${confirm.label}.` });
      setConfirm(null);
      load();
    } catch (err: any) {
      const d = err.response?.data?.detail;
      setToast({ tone: "danger", message: typeof d === "string" ? d : "Update failed" });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-6">
      <PageHeader
        title="Hotlist"
        subtitle="Vehicles police are actively watching. Every sighting from a volunteer scanner lands on the live alert stream."
      />

      <div className="space-y-4">
        <div className="relative max-w-sm">
          <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-surface-muted" aria-hidden />
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search plate or FIR number"
            aria-label="Search hotlist"
            className="h-10 w-full rounded-lg border border-surface-border bg-surface-card pl-9 pr-3 text-sm text-surface-text placeholder:text-surface-subtle focus:border-primary-600 focus:outline-none focus:ring-2 focus:ring-primary-600/25"
          />
        </div>

        <div className="scrollbar-thin -mx-1 flex gap-2 overflow-x-auto px-1 pb-1">
          {FILTERS.map((f) => (
            <button
              key={f.value}
              type="button"
              onClick={() => setFilter(f.value)}
              aria-pressed={filter === f.value}
              className={[
                "whitespace-nowrap rounded-full px-3 py-1.5 text-xs font-medium transition-colors",
                filter === f.value
                  ? "bg-solid text-white"
                  : "border border-surface-border bg-surface-card text-surface-muted hover:border-primary-300 hover:text-surface-text",
              ].join(" ")}
            >
              {f.label}
            </button>
          ))}
        </div>
      </div>

      {error ? (
        <Card padding="none">
          <ErrorState title="Could not load the hotlist" description={error} onRetry={load} />
        </Card>
      ) : loading ? (
        <Card padding="none">
          <div className="space-y-3 p-5">
            {[0, 1, 2, 3].map((i) => (
              <Skeleton key={i} className="h-12" />
            ))}
          </div>
        </Card>
      ) : rows.length === 0 ? (
        <Card padding="none">
          <EmptyState
            icon={<ShieldAlert className="h-6 w-6" aria-hidden />}
            title="Nothing on the hotlist"
            description={
              filter === "ALL"
                ? "Verify a citizen complaint to add the first vehicle to the hotlist."
                : `No hotlist entries with status ${filter.replace(/_/g, " ").toLowerCase()}.`
            }
            action={
              filter !== "ALL" ? (
                <Button variant="secondary" onClick={() => setFilter("ALL")}>
                  Show all
                </Button>
              ) : undefined
            }
          />
        </Card>
      ) : (
        <Card padding="none" className="overflow-hidden">
          <Table className="rounded-none border-0">
            <THead>
              <TH>Plate</TH>
              <TH>Status</TH>
              <TH className="hidden md:table-cell">FIR</TH>
              <TH className="hidden lg:table-cell">Added</TH>
              <TH className="hidden xl:table-cell">Last seen</TH>
              <TH className="text-right">Action</TH>
            </THead>
            <TBody>
              {rows.map((h) => (
                <TR key={h.id}>
                  <TD>
                    <Link to={platePath(h.plate)} className="inline-block hover:opacity-80">
                      <PlateBadge plate={h.plate} size="sm" />
                    </Link>
                    {h.last_seen_lat != null ? (
                      <p className="mt-1.5 text-xs text-surface-muted">
                        {formatCoords(h.last_seen_lat, h.last_seen_lng)}
                      </p>
                    ) : null}
                  </TD>
                  <TD>
                    <HotlistChip status={h.status} />
                  </TD>
                  <TD className="hidden font-mono text-xs text-surface-muted md:table-cell">
                    {h.fir_reference || "—"}
                  </TD>
                  <TD className="hidden whitespace-nowrap text-sm text-surface-muted lg:table-cell">
                    {timeAgo(h.added_at)}
                  </TD>
                  <TD className="hidden whitespace-nowrap text-sm text-surface-muted xl:table-cell">
                    {h.last_seen_at ? timeAgo(h.last_seen_at) : "—"}
                  </TD>
                  <TD className="text-right">
                    {TRANSITIONS[h.status]?.length ? (
                      <div className="flex flex-wrap justify-end gap-2">
                        {TRANSITIONS[h.status].map((t) => (
                          <Button
                            key={t.status}
                            size="sm"
                            variant={t.variant}
                            onClick={() => setConfirm({ entry: h, status: t.status, label: t.label })}
                          >
                            {t.label}
                          </Button>
                        ))}
                      </div>
                    ) : (
                      <Link
                        to={platePath(h.plate)}
                        className="text-sm font-medium text-primary-600 hover:text-primary-700"
                      >
                        View →
                      </Link>
                    )}
                  </TD>
                </TR>
              ))}
            </TBody>
          </Table>
          <Pagination
            page={page}
            pageCount={pageCount}
            total={filtered.length}
            pageSize={PAGE_SIZE}
            onPage={setPage}
          />
        </Card>
      )}

      <Modal
        open={confirm !== null}
        onClose={() => !busy && setConfirm(null)}
        title={confirm?.label ?? ""}
        description="This changes the hotlist status for every officer viewing the console."
        size="sm"
        footer={
          <>
            <Button variant="secondary" onClick={() => setConfirm(null)} disabled={busy}>
              Cancel
            </Button>
            <Button variant="primary" loading={busy} onClick={patch}>
              Confirm
            </Button>
          </>
        }
      >
        <p className="text-sm text-surface-muted">
          Vehicle <span className="font-mono font-bold text-surface-text">{confirm?.entry.plate}</span> will be
          marked{" "}
          <span className="font-semibold text-surface-text">
            {confirm?.status.replace(/_/g, " ").toLowerCase()}
          </span>
          .
        </p>
      </Modal>

      <Toast
        open={toast !== null}
        tone={toast?.tone}
        message={toast?.message || ""}
        onClose={() => setToast(null)}
      />
    </div>
  );
}
