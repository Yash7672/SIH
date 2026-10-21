import { useCallback, useEffect, useState } from "react";
import {
  api,
  createPoliceAccount,
  resetUserPassword,
  setUserActive,
  User,
} from "../services/api";
import { Button } from "../components/ui/Button";
import { Card, CardHeader } from "../components/ui/Card";
import { Badge } from "../components/ui/StatusChip";
import { Modal, PageHeader, Toast } from "../components/ui/Overlay";
import { Input } from "../components/ui/Input";
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

  // The signed-in admin, so the table can mark "you" and the UI never offers to
  // deactivate the account you are using. Read the same way RequireAuth does.
  const meId = (() => {
    try {
      return (JSON.parse(localStorage.getItem("rakshak_user") || "null") as User | null)?.id ?? null;
    } catch {
      return null;
    }
  })();

  // Create-police-account form.
  const [createOpen, setCreateOpen] = useState(false);
  const [form, setForm] = useState({ name: "", email: "", phone: "", password: "" });
  const [formError, setFormError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  // Reset-password dialog.
  const [resetFor, setResetFor] = useState<User | null>(null);
  const [newPassword, setNewPassword] = useState("");
  const [resetError, setResetError] = useState<string | null>(null);

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

  function openCreate() {
    setForm({ name: "", email: "", phone: "", password: "" });
    setFormError(null);
    setCreateOpen(true);
  }

  async function submitCreate(e: React.FormEvent) {
    e.preventDefault();
    setFormError(null);
    // Mirror the server's bound so the admin is told before the round trip.
    if (form.password.length < 8) {
      setFormError("Password must be at least 8 characters.");
      return;
    }
    setCreating(true);
    try {
      const created = await createPoliceAccount({
        name: form.name.trim(),
        email: form.email.trim(),
        phone: form.phone.trim() || undefined,
        password: form.password,
      });
      setCreateOpen(false);
      setToast({
        tone: "success",
        message: `Police account created for ${created.email}. Share the password with the station.`,
      });
      load();
    } catch (err: any) {
      const d = err.response?.data?.detail;
      setFormError(typeof d === "string" ? d : "Could not create the account.");
    } finally {
      setCreating(false);
    }
  }

  async function submitReset(e: React.FormEvent) {
    e.preventDefault();
    if (!resetFor) return;
    setResetError(null);
    if (newPassword.length < 8) {
      setResetError("Password must be at least 8 characters.");
      return;
    }
    setBusy(resetFor.id);
    try {
      await resetUserPassword(resetFor.id, newPassword);
      setResetFor(null);
      setNewPassword("");
      setToast({ tone: "success", message: `Password reset for ${resetFor.email}.` });
    } catch (err: any) {
      const d = err.response?.data?.detail;
      setResetError(typeof d === "string" ? d : "Could not reset the password.");
    } finally {
      setBusy(null);
    }
  }

  async function toggleActive(u: User) {
    setBusy(u.id);
    try {
      const updated = await setUserActive(u.id, !(u.active ?? true));
      setToast({
        tone: "success",
        message: `${updated.email} ${updated.active ? "reactivated" : "deactivated"}.`,
      });
      load();
    } catch (err: any) {
      const d = err.response?.data?.detail;
      setToast({ tone: "danger", message: typeof d === "string" ? d : "Could not update the account." });
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
        action={
          <Button onClick={openCreate} disabled={loading}>
            Create police account
          </Button>
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
              <TH>Status</TH>
              <TH className="hidden lg:table-cell">Joined</TH>
              <TH className="text-right">Actions</TH>
            </THead>
            <TBody>
              {users.map((u) => {
                const isActive = u.active ?? true;
                const isSelf = u.id === meId;
                // Password reset is offered only where the server allows it:
                // police and admin accounts. A citizen password reset from this
                // console would be an account takeover.
                const canReset = u.role === "COP" || u.role === "ADMIN";
                return (
                  <TR key={u.id}>
                    <TD className="font-medium">
                      {u.name}
                      {isSelf ? <span className="ml-2 text-xs text-surface-muted">(you)</span> : null}
                    </TD>
                    <TD className="hidden text-surface-muted sm:table-cell">{u.email}</TD>
                    <TD>
                      <Badge tone={ROLE_TONES[u.role] || "neutral"}>{u.role}</Badge>
                    </TD>
                    <TD>
                      <Badge tone={isActive ? "success" : "danger"}>{isActive ? "Active" : "Disabled"}</Badge>
                    </TD>
                    <TD className="hidden whitespace-nowrap text-surface-muted lg:table-cell">
                      {u.created_at ? new Date(u.created_at).toLocaleDateString() : "—"}
                    </TD>
                    <TD className="text-right">
                      <div className="flex justify-end gap-2">
                        {canReset ? (
                          <Button
                            size="sm"
                            variant="secondary"
                            onClick={() => {
                              setNewPassword("");
                              setResetError(null);
                              setResetFor(u);
                            }}
                            disabled={busy === u.id}
                          >
                            Reset password
                          </Button>
                        ) : null}
                        {isSelf ? (
                          <span className="text-xs text-surface-subtle">—</span>
                        ) : (
                          <Button
                            size="sm"
                            variant={isActive ? "danger" : "secondary"}
                            loading={busy === u.id}
                            onClick={() => toggleActive(u)}
                          >
                            {isActive ? "Deactivate" : "Reactivate"}
                          </Button>
                        )}
                      </div>
                    </TD>
                  </TR>
                );
              })}
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
        open={createOpen}
        onClose={() => !creating && setCreateOpen(false)}
        title="Create police account"
        description="Only an administrator can create a police account. The officer signs in with the email and password you set here."
        size="md"
        footer={
          <>
            <Button variant="secondary" onClick={() => setCreateOpen(false)} disabled={creating}>
              Cancel
            </Button>
            <Button type="submit" form="create-police-form" loading={creating}>
              Create account
            </Button>
          </>
        }
      >
        <form id="create-police-form" onSubmit={submitCreate} className="space-y-4">
          <Input
            label="Full name"
            required
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
            placeholder="Inspector R. Sharma"
          />
          <Input
            label="Email"
            type="email"
            required
            value={form.email}
            onChange={(e) => setForm({ ...form, email: e.target.value })}
            placeholder="station.hyderabad@police.gov.in"
            hint="This is the sign-in name the officer uses."
          />
          <Input
            label="Phone"
            value={form.phone}
            onChange={(e) => setForm({ ...form, phone: e.target.value })}
            placeholder="Optional"
          />
          <Input
            label="Password"
            type="password"
            required
            minLength={8}
            autoComplete="new-password"
            value={form.password}
            onChange={(e) => setForm({ ...form, password: e.target.value })}
            placeholder="At least 8 characters"
            hint="Share this with the station. You can reset it later if it is lost."
          />
          {formError ? (
            <div
              role="alert"
              className="rounded-lg border border-danger/40 bg-danger/10 px-3.5 py-2.5 text-sm font-medium text-danger"
            >
              {formError}
            </div>
          ) : null}
        </form>
      </Modal>

      <Modal
        open={resetFor !== null}
        onClose={() => !busy && setResetFor(null)}
        title="Reset password"
        description="Set a new password for this account and pass it on. The old password stops working immediately."
        size="sm"
        footer={
          <>
            <Button variant="secondary" onClick={() => setResetFor(null)} disabled={!!busy}>
              Cancel
            </Button>
            <Button type="submit" form="reset-password-form" loading={busy === resetFor?.id}>
              Reset password
            </Button>
          </>
        }
      >
        <form id="reset-password-form" onSubmit={submitReset} className="space-y-4">
          <p className="text-sm text-surface-muted">
            Account <span className="font-semibold text-surface-text">{resetFor?.email}</span> ({resetFor?.role})
          </p>
          <Input
            label="New password"
            type="password"
            required
            minLength={8}
            autoComplete="new-password"
            value={newPassword}
            onChange={(e) => setNewPassword(e.target.value)}
            placeholder="At least 8 characters"
          />
          {resetError ? (
            <div
              role="alert"
              className="rounded-lg border border-danger/40 bg-danger/10 px-3.5 py-2.5 text-sm font-medium text-danger"
            >
              {resetError}
            </div>
          ) : null}
        </form>
      </Modal>

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
