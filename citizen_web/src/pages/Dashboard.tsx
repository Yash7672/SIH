import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { AlertTriangle, ClipboardList, FilePlus2, ShieldAlert } from "lucide-react";
import { api, Complaint } from "../services/api";
import { Button } from "../components/ui/Button";
import { Card, CardHeader } from "../components/ui/Card";
import { PlateBadge, StatusChip } from "../components/ui/StatusChip";
import { StatCard } from "../components/ui/StatCard";
import { EmptyState, ErrorState, PageSkeleton } from "../components/ui/Feedback";
import { PageHeader } from "../components/ui/Overlay";

export default function Dashboard() {
  const navigate = useNavigate();
  const [complaints, setComplaints] = useState<Complaint[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    const controller = new AbortController();
    api
      .get<Complaint[]>("/complaints/mine", { signal: controller.signal })
      .then((r) => setComplaints(r.data))
      .catch((e) => {
        if (e.code !== "ERR_CANCELED") setError(e.response?.data?.detail || "Could not load your complaints");
      })
      .finally(() => setLoading(false));
    return () => controller.abort();
  }, []);

  useEffect(load, [load]);

  const user = JSON.parse(localStorage.getItem("rakshak_user") || "null");
  const firstName = user?.name ? String(user.name).split(" ")[0] : null;
  const pending = complaints.filter((c) => c.status === "PENDING" || c.status === "UNDER_REVIEW").length;
  const hotlisted = complaints.filter((c) => c.status === "HOTLISTED").length;
  const resolved = complaints.filter((c) => c.status === "VERIFIED" || c.status === "CLOSED").length;

  if (loading) return <PageSkeleton />;
  if (error) return <ErrorState title="Could not load your dashboard" description={error} onRetry={load} />;

  return (
    <div className="space-y-6">
      <PageHeader
        title={firstName ? `Hello, ${firstName}` : "Dashboard"}
        subtitle="Track the vehicles you have reported and what police have done with them."
        action={
          <Button
            size="md"
            icon={<FilePlus2 className="h-4 w-4" aria-hidden />}
            onClick={() => navigate("/complaints/new")}
          >
            Report a vehicle
          </Button>
        }
      />

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
        <StatCard
          label="Total complaints"
          value={complaints.length}
          icon={<ClipboardList className="h-5 w-5" aria-hidden />}
          tone="primary"
          hint="filed by you"
        />
        <StatCard
          label="In review"
          value={pending}
          icon={<AlertTriangle className="h-5 w-5" aria-hidden />}
          tone="warning"
          hint="awaiting a decision"
        />
        <StatCard
          label="Hotlisted"
          value={hotlisted}
          icon={<ShieldAlert className="h-5 w-5" aria-hidden />}
          tone="danger"
          hint="active alerts to police"
        />
      </div>

      <Card padding="none">
        <div className="p-5 pb-0">
          <CardHeader
            title="Recent complaints"
            subtitle={resolved > 0 ? `${resolved} resolved so far` : undefined}
            action={
              <Link
                to="/complaints"
                className="text-sm font-medium text-primary-600 hover:text-primary-700"
              >
                View all
              </Link>
            }
          />
        </div>

        {complaints.length === 0 ? (
          <EmptyState
            icon={<ClipboardList className="h-6 w-6" aria-hidden />}
            title="No complaints yet"
            description="Report a stolen or missing vehicle and police will verify it before it enters the hotlist."
            action={
              <Link
                to="/complaints/new"
                className="inline-flex h-10 items-center rounded-lg bg-solid px-4 text-sm font-medium text-white shadow-sm transition-colors hover:bg-solid-hover"
              >
                Report your first vehicle
              </Link>
            }
          />
        ) : (
          <ul className="mt-4 divide-y divide-surface-border">
            {complaints.slice(0, 5).map((c) => (
              <li key={c.id} className="flex flex-wrap items-center justify-between gap-3 px-5 py-4 transition-colors hover:bg-surface-bg">
                <div className="min-w-0">
                  <Link to={`/complaints/${c.id}`} className="inline-block hover:opacity-80">
                    <PlateBadge plate={c.plate} size="sm" />
                  </Link>
                  <p className="mt-1.5 text-xs text-surface-muted">
                    {String(c.complaint_type).replace(/_/g, " ")} ·{" "}
                    {new Date(c.created_at).toLocaleString()}
                  </p>
                </div>
                <StatusChip status={c.status} />
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}