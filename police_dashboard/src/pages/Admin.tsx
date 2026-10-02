import { useCallback, useEffect, useState } from "react";
import { api, User } from "../services/api";
import { Button } from "../components/ui/Button";
import { Card, CardHeader } from "../components/ui/Card";
import { Badge } from "../components/ui/StatusChip";
import { Modal, PageHeader, Toast } from "../components/ui/Overlay";
import { TD, TH, TBody, THead, TR, Table } from "../components/ui/Table";
import { EmptyState, Skeleton } from "../components/ui/Feedback";
import { timeAgo } from "../lib/format";

interface Device {
  id: string;
  user_id: string;
  device_type: string;
  device_name?: string;
  revoked: boolean;
  last_seen_at?: string;
  created_at: string;
}

const ROLE_TONES: Record<string, "info" | "success" | "primary" | "danger"> = {
  CITIZEN: "info",
  VOLUNTEER: "success",
  COP: "primary",
  ADMIN: "danger",
};

export default function Admin() {
  const [users, setUsers] = useState<User[]>([]);
  const [devices, setDevices] = useState<Device[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<Device | null>(null);
  const [toast, setToast] = useState<{ tone: "success" | "danger"; message: string } | null>(null);

  const load = useCallback(() => {
    setLoading(true);
    Promise.allSettled([api.get<User[]>("/users"), api.get<Device[]>("/devices")])
      .then(([u, d]) => {
        if (u.status === "fulfilled") setUsers(u.value.data);
        if (d.status === "fulfilled") setDevices(d.value.data);
        if (u.status === "rejected" && d.status === "rejected") setError("Could not load administration data.");
      })
      .finally(() => setLoading(false));
  }, []);

  useEffect(load, [load]);

  async function revoke(device: Device) {
    setBusy(device.id);
    try {
      await api.post(`/devices/${device.id}/revoke`);
      setToast({ tone: "success", message: `${device.device_name || device.id.slice(0, 8)} revoked.` });
      setConfirm(null);
      load();
    } catch (e: any) {
      const d = e.response?.data?.detail;
      setToast({ tone: "danger", message: typeof d === "string" ? d : "Revoke failed" });
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="space-y-6">
      <PageHeader
        title="Administration"
        subtitle={
          loading ? "Loading…" : `${users.length} user${users.length === 1 ? "" : "s"} · ${devices.length} device${devices.length === 1 ? "" : "s"}`
        }
      />

      {error ? (
        <div role="alert" className="rounded-xl border border-danger/40 bg-danger/10 p-4 text-sm text-danger">
          {error}
        </div>
      ) : null}

      <Card padding="none" className="overflow-hidden">
        <div className="p-5 pb-0">
          <CardHeader title="Registered users" subtitle="Everyone with an account on this deployment." />
        </div>
        {loading ? (
          <div className="space-y-3 p-5">
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} className="h-10" />
            ))}
          </div>
        ) : users.length === 0 ? (
          <EmptyState title="No users" description="Registered accounts will appear here." />
        ) : (
          <Table className="mt-4 rounded-none border-0">
            <THead>
              <TH>Name</TH>
              <TH className="hidden sm:table-cell">Email</TH>
              <TH>Role</TH>
              <TH className="hidden lg:table-cell">Joined</TH>
            </THead>
            <TBody>
              {users.map((u) => (
                <TR key={u.id}>
                  <TD className="font-medium">{u.name}</TD>
                  <TD className="hidden text-surface-muted sm:table-cell">{u.email}</TD>
                  <TD>
                    <Badge tone={ROLE_TONES[u.role] || "neutral"}>{u.role}</Badge>
                  </TD>
                  <TD className="hidden whitespace-nowrap text-surface-muted lg:table-cell">
                    {u.created_at ? new Date(u.created_at).toLocaleDateString() : "—"}
                  </TD>
                </TR>
              ))}
            </TBody>
          </Table>
        )}
      </Card>

      <Card padding="none" className="overflow-hidden">
        <div className="p-5 pb-0">
          <CardHeader
            title="Registered devices"
            subtitle="ANPR capture devices contributed by volunteers."
          />
        </div>
        {loading ? (
          <div className="space-y-3 p-5">
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} className="h-10" />
            ))}
          </div>
        ) : devices.length === 0 ? (
          <EmptyState
            title="No devices registered"
            description="Devices appear here once a volunteer signs in from the mobile app."
          />
        ) : (
          <Table className="mt-4 rounded-none border-0">
            <THead>
              <TH>Name</TH>
              <TH className="hidden sm:table-cell">Type</TH>
              <TH className="hidden md:table-cell">Owner</TH>
              <TH className="hidden lg:table-cell">Last seen</TH>
              <TH>Status</TH>
              <TH className="text-right">Action</TH>
            </THead>
            <TBody>
              {devices.map((d) => (
                <TR key={d.id}>
                  <TD className="font-medium">{d.device_name || "—"}</TD>
                  <TD className="hidden capitalize text-surface-muted sm:table-cell">{d.device_type}</TD>
                  <TD className="hidden font-mono text-xs text-surface-muted md:table-cell">
                    {d.user_id.slice(0, 8)}…
                  </TD>
                  <TD className="hidden whitespace-nowrap text-surface-muted lg:table-cell">
                    {d.last_seen_at ? timeAgo(d.last_seen_at) : "never"}
                  </TD>
                  <TD>
                    <Badge tone={d.revoked ? "danger" : "success"}>
                      {d.revoked ? "Revoked" : "Active"}
                    </Badge>
                  </TD>
                  <TD className="text-right">
                    {!d.revoked ? (
                      <Button size="sm" variant="danger" onClick={() => setConfirm(d)}>
                        Revoke
                      </Button>
                    ) : (
                      <span className="text-xs text-surface-subtle">—</span>
                    )}
                  </TD>
                </TR>
              ))}
            </TBody>
          </Table>
        )}
      </Card>

      <Modal
        open={confirm !== null}
        onClose={() => !busy && setConfirm(null)}
        title="Revoke device"
        description="The device stops being able to upload sightings. This cannot be undone from the console."
        size="sm"
        footer={
          <>
            <Button variant="secondary" onClick={() => setConfirm(null)} disabled={!!busy}>
              Cancel
            </Button>
            <Button variant="danger" loading={!!busy} onClick={() => confirm && revoke(confirm)}>
              Revoke device
            </Button>
          </>
        }
      >
        <p className="text-sm text-surface-muted">
          Device <span className="font-semibold text-surface-text">{confirm?.device_name || confirm?.id}</span> (
          {confirm?.device_type}) will lose access immediately.
        </p>
      </Modal>

      <Toast open={toast !== null} tone={toast?.tone} message={toast?.message || ""} onClose={() => setToast(null)} />
    </div>
  );
}
