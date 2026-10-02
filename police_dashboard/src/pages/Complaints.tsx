import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { ListChecks, Search } from "lucide-react";
import { api, Complaint } from "../services/api";
import { Button } from "../components/ui/Button";
import { Card } from "../components/ui/Card";
import { Modal } from "../components/ui/Overlay";
import { Toast } from "../components/ui/Overlay";
import { PlateBadge, StatusChip } from "../components/ui/StatusChip";
import { Pagination } from "../components/ui/Pagination";
import { TD, TH, TBody, THead, TR, Table } from "../components/ui/Table";
import { EmptyState, ErrorState, Skeleton } from "../components/ui/Feedback";
import { PageHeader } from "../components/ui/Overlay";

const PAGE_SIZE = 50;

const FILTERS = [
  { value: "ALL", label: "All" },
  { value: "PENDING", label: "Pending" },
  { value: "UNDER_REVIEW", label: "In review" },
  { value: "VERIFIED", label: "Verified" },
  { value: "HOTLISTED", label: "Hotlisted" },
  { value: "REJECTED", label: "Rejected" },
  { value: "CLOSED", label: "Closed" },
];

type PendingAction = { id: string; plate: string; kind: "verify" | "reject" } | null;

function useDebounced<T>(value: T, delay = 300) {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), delay);
    return () => clearTimeout(t);
  }, [value, delay]);
  return debounced;
}

export default function Complaints() {
  const [items, setItems] = useState<Complaint[]>([]);
  const [filter, setFilter] = useState("ALL");
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<PendingAction>(null);
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState<{ tone: "success" | "danger"; message: string } | null>(null);
  const debouncedQuery = useDebounced(query);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    const q = filter === "ALL" ? "" : `?status_filter=${filter}`;
    api
      .get<Complaint[]>(`/complaints${q}`)
      .then((r) => setItems(r.data))
      .catch((e) => {
        const d = e.response?.data?.detail;
        setError(typeof d === "string" ? d : "Could not load complaints");
      })
      .finally(() => setLoading(false));
  }, [filter]);

  useEffect(load, [load]);

  // Any filter or search change invalidates the current page offset.
  useEffect(() => setPage(0), [filter, debouncedQuery]);

  const filtered = useMemo(() => {
    const q = debouncedQuery.trim().toUpperCase();
    if (!q) return items;
    return items.filter(
      (c) =>
        c.plate.toUpperCase().includes(q) ||
        String(c.complaint_type).toUpperCase().includes(q) ||
        (c.description || "").toUpperCase().includes(q)
    );
  }, [items, debouncedQuery]);

  const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const rows = useMemo(
    () => filtered.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE),
    [filtered, page]
  );

  async function runAction(action: NonNullable<PendingAction>) {
    setBusy(true);
    try {
      await api.post(`/complaints/${action.id}/${action.kind}`, {});
      setToast({
        tone: "success",
        message:
          action.kind === "verify"
            ? `${action.plate} verified and added to the hotlist.`
            : `${action.plate} rejected.`,
      });
      setConfirm(null);
      load();
    } catch (err: any) {
      const d = err.response?.data?.detail;
      setToast({ tone: "danger", message: typeof d === "string" ? d : "Action failed" });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-6">
      <PageHeader
        title="Complaints"
        subtitle="Citizen reports awaiting triage. Verifying a complaint puts the vehicle on the hotlist and starts alerting volunteers."
      />

      <div className="space-y-4">
        <div className="relative max-w-sm">
          <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-surface-muted" aria-hidden />
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search plate or description"
            aria-label="Search complaints"
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
          <ErrorState title="Could not load complaints" description={error} onRetry={load} />
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
            icon={<ListChecks className="h-6 w-6" aria-hidden />}
            title="No complaints here"
            description={
              filter === "ALL"
                ? "No citizen has filed a complaint yet."
                : `No complaints with status ${filter.replace(/_/g, " ").toLowerCase()}.`
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
              <TH>Type</TH>
              <TH>Status</TH>
              <TH className="hidden lg:table-cell">Submitted</TH>
              <TH className="text-right">Action</TH>
            </THead>
            <TBody>
              {rows.map((c) => (
                <TR key={c.id}>
                  <TD>
                    <PlateBadge plate={c.plate} size="sm" />
                  </TD>
                  <TD className="capitalize">{String(c.complaint_type).replace(/_/g, " ")}</TD>
                  <TD>
                    <StatusChip status={c.status} />
                  </TD>
                  <TD className="hidden whitespace-nowrap text-sm text-surface-muted lg:table-cell">
                    {new Date(c.created_at).toLocaleString()}
                  </TD>
                  <TD className="text-right">
                    {c.status === "PENDING" || c.status === "UNDER_REVIEW" ? (
                      <div className="flex justify-end gap-2">
                        <Button
                          size="sm"
                          variant="secondary"
                          className="border-success/40 text-success hover:bg-success/10"
                          onClick={() => setConfirm({ id: c.id, plate: c.plate, kind: "verify" })}
                        >
                          Verify
                        </Button>
                        <Button
                          size="sm"
                          variant="danger"
                          onClick={() => setConfirm({ id: c.id, plate: c.plate, kind: "reject" })}
                        >
                          Reject
                        </Button>
                      </div>
                    ) : c.status === "HOTLISTED" ? (
                      <Link
                        to={`/vehicles/${encodeURIComponent(c.plate)}`}
                        className="text-sm font-medium text-primary-600 hover:text-primary-700"
                      >
                        View vehicle →
                      </Link>
                    ) : (
                      <span className="text-xs text-surface-subtle">—</span>
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
        title={confirm?.kind === "verify" ? "Verify complaint" : "Reject complaint"}
        description={
          confirm?.kind === "verify"
            ? "This adds the vehicle to the active hotlist. Every volunteer scanner will start alerting police on sight."
            : "The vehicle will not be added to the hotlist. The citizen is notified."
        }
        size="sm"
        footer={
          <>
            <Button variant="secondary" onClick={() => setConfirm(null)} disabled={busy}>
              Cancel
            </Button>
            <Button
              variant={confirm?.kind === "verify" ? "primary" : "danger"}
              loading={busy}
              onClick={() => confirm && runAction(confirm)}
            >
              {confirm?.kind === "verify" ? "Verify and hotlist" : "Reject complaint"}
            </Button>
          </>
        }
      >
        <p className="text-sm text-surface-muted">
          Plate <span className="font-mono font-bold text-surface-text">{confirm?.plate}</span>
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
