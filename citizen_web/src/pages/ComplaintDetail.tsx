import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  BadgeCheck,
  Check,
  ChevronLeft,
  RefreshCw,
  ShieldAlert,
  ShieldCheck,
  X,
} from "lucide-react";
import { api, Complaint } from "../services/api";
import { Button } from "../components/ui/Button";
import { Card, CardHeader } from "../components/ui/Card";
import { ErrorState, PageSkeleton } from "../components/ui/Feedback";
import { PlateBadge, StatusChip } from "../components/ui/StatusChip";

const TIMELINE: Record<string, { steps: string[]; outcome: "open" | "verified" | "hotlisted" | "rejected" }> = {
  PENDING: { steps: ["Submitted"], outcome: "open" },
  UNDER_REVIEW: { steps: ["Submitted", "Under review"], outcome: "open" },
  VERIFIED: { steps: ["Submitted", "Under review", "Verified"], outcome: "verified" },
  HOTLISTED: { steps: ["Submitted", "Under review", "Verified", "Hotlisted"], outcome: "hotlisted" },
  CLOSED: { steps: ["Submitted", "Under review", "Verified", "Hotlisted", "Closed"], outcome: "verified" },
  REJECTED: { steps: ["Submitted", "Under review", "Rejected"], outcome: "rejected" },
};

/** The short explanation under the timeline, one per verification outcome. */
function OutcomeNote({ status }: { status: string }) {
  const { outcome } = TIMELINE[status] || { outcome: "open" as const };

  if (outcome === "rejected") {
    return (
      <div className="rounded-xl border border-danger/40 bg-danger/10 p-4 text-sm text-danger">
        <p className="flex items-center gap-2 font-semibold">
          <X className="h-4 w-4" aria-hidden /> Not added to the hotlist
        </p>
        <p className="mt-1">
          Police could not verify this report. If you have more evidence, contact the station directly with the
          complaint ID.
        </p>
      </div>
    );
  }

  if (outcome === "hotlisted") {
    return (
      <div className="rounded-xl border border-danger/40 bg-danger/10 p-4 text-sm text-danger">
        <p className="flex items-center gap-2 font-semibold">
          <ShieldAlert className="h-4 w-4" aria-hidden /> Active hotlist entry
        </p>
        <p className="mt-1">
          Volunteers&apos; scanners now flag sightings of this vehicle and alert police in real time.
        </p>
      </div>
    );
  }

  if (outcome === "verified") {
    return (
      <div className="rounded-xl border border-success/40 bg-success/10 p-4 text-sm text-success">
        <p className="flex items-center gap-2 font-semibold">
          <BadgeCheck className="h-4 w-4" aria-hidden /> Verified by police
        </p>
        <p className="mt-1">
          Thank you - this report was confirmed and passed to the operations team.
        </p>
      </div>
    );
  }

  return (
    <div className="rounded-xl border border-surface-border bg-surface-raised p-4 text-sm text-surface-muted">
      Police are still verifying this report. Most complaints are reviewed within 24 hours.
    </div>
  );
}

export default function ComplaintDetail() {
  const { id } = useParams();
  const [complaint, setComplaint] = useState<Complaint | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    setError(null);
    api
      .get<Complaint>(`/complaints/${id}`)
      .then((r) => setComplaint(r.data))
      .catch((e) => {
        const detail = e.response?.data?.detail;
        setError(typeof detail === "string" ? detail : "Could not load this complaint");
      });
  }, [id]);

  useEffect(load, [load]);

  if (error) {
    return (
      <div className="mx-auto max-w-2xl space-y-4">
        <Link
          to="/complaints"
          className="inline-flex items-center gap-1.5 text-sm font-medium text-primary-600 hover:text-primary-700"
        >
          <ChevronLeft className="h-4 w-4" aria-hidden /> Back to my complaints
        </Link>
        <Card padding="none">
          <ErrorState title="Could not load this complaint" description={error} onRetry={load} />
        </Card>
      </div>
    );
  }

  if (!complaint) return <PageSkeleton rows={3} />;

  const flow = TIMELINE[complaint.status] || TIMELINE.PENDING;
  const reached = flow.steps.length;

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <Link
        to="/complaints"
        className="inline-flex items-center gap-1.5 text-sm font-medium text-primary-600 hover:text-primary-700"
      >
        <ChevronLeft className="h-4 w-4" aria-hidden /> Back to my complaints
      </Link>

      <div className="flex flex-wrap items-center justify-between gap-4">
        <div className="min-w-0">
          <PlateBadge plate={complaint.plate} size="lg" />
          <p className="mt-2.5 text-xs text-surface-muted">
            Complaint ID <code className="font-mono text-surface-text">{complaint.id}</code>
          </p>
        </div>
        <StatusChip status={complaint.status} className="px-3 py-1.5 text-sm" />
      </div>

      <Card padding="none">
        <div className="p-5 pb-0">
          <CardHeader title="Status timeline" subtitle="Each step is recorded by the verifying officer." />
        </div>
        <ol className="mt-5 space-y-0 px-5 pb-5">
          {flow.steps.map((step, i) => {
            const done = i < reached;
            const current = i === flow.steps.length - 1;
            const rejected = flow.outcome === "rejected" && current;
            return (
              <li key={step} className="relative flex gap-4 pb-5 last:pb-0">
                {current ? null : (
                  <span
                    className={[
                      "absolute left-[13px] top-7 h-full w-0.5",
                      done && !rejected ? "bg-primary-200" : "bg-surface-border",
                    ].join(" ")}
                    aria-hidden
                  />
                )}
                <span
                  className={[
                    "z-10 flex h-7 w-7 shrink-0 items-center justify-center rounded-full",
                    rejected
                      ? "bg-danger/15 text-danger"
                      : done
                        ? "bg-solid text-white"
                        : "bg-surface-raised text-surface-subtle",
                  ].join(" ")}
                  aria-hidden
                >
                  {done ? <Check className="h-4 w-4" /> : <ShieldCheck className="h-3 w-3" />}
                </span>
                <div className="min-w-0 pt-0.5">
                  <p
                    className={[
                      "text-sm font-medium",
                      rejected ? "text-danger" : done ? "text-surface-text" : "text-surface-muted",
                    ].join(" ")}
                  >
                    {step}
                  </p>
                  {current ? <p className="mt-0.5 text-xs text-surface-muted">Current status</p> : null}
                </div>
              </li>
            );
          })}
        </ol>
      </Card>

      <OutcomeNote status={complaint.status} />

      <Card>
        <CardHeader title="Report details" />
        <dl className="mt-4 grid grid-cols-1 gap-4 text-sm sm:grid-cols-2">
          <div>
            <dt className="text-surface-muted">Type</dt>
            <dd className="mt-0.5 font-medium capitalize text-surface-text">
              {String(complaint.complaint_type).replace(/_/g, " ")}
            </dd>
          </div>
          <div>
            <dt className="text-surface-muted">Submitted</dt>
            <dd className="mt-0.5 font-medium text-surface-text">
              {new Date(complaint.created_at).toLocaleString()}
            </dd>
          </div>
          <div>
            <dt className="text-surface-muted">Last updated</dt>
            <dd className="mt-0.5 font-medium text-surface-text">
              {new Date(complaint.updated_at).toLocaleString()}
            </dd>
          </div>
          <div className="sm:col-span-2">
            <dt className="text-surface-muted">Description</dt>
            <dd className="mt-0.5 whitespace-pre-line text-surface-text">
              {complaint.description || "No description provided."}
            </dd>
          </div>
        </dl>
      </Card>

      <div className="flex justify-end">
        <Button variant="secondary" onClick={load} icon={<RefreshCw className="h-3.5 w-3.5" />}>
          Refresh status
        </Button>
      </div>
    </div>
  );
}
