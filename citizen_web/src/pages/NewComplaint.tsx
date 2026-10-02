import { useCallback, useMemo, useRef, useState, type ChangeEvent, type DragEvent } from "react";
import { useNavigate } from "react-router-dom";
import { FileImage, Trash2, Upload, UploadCloud } from "lucide-react";
import { api } from "../services/api";
import { Button } from "../components/ui/Button";
import { Card } from "../components/ui/Card";
import { Input, Select } from "../components/ui/Input";
import { ErrorState } from "../components/ui/Feedback";
import { PageHeader } from "../components/ui/Overlay";

const TYPES = [
  { value: "stolen", label: "Stolen vehicle" },
  { value: "missing", label: "Missing vehicle" },
  { value: "hit_and_run", label: "Hit and run" },
  { value: "suspicious", label: "Suspicious activity" },
];

const MAX_BYTES = 5 * 1024 * 1024;
const ACCEPT = ".jpg,.jpeg,.png,.pdf";

/** Live counter for the registration field: how many characters are still missing. */
function plateHint(raw: string) {
  const clean = raw.replace(/[^A-Za-z0-9]/g, "");
  if (clean.length === 0) return "Format: 2 letters, 1-2 digits, 1-2 letters, 4 digits, e.g. MH12AB1234";
  if (clean.length === 10) return "Looks complete - 10 characters.";
  return `${10 - clean.length} more character(s) to complete.`;
}

export default function NewComplaint() {
  const nav = useNavigate();
  const [plate, setPlate] = useState("");
  const [type, setType] = useState("stolen");
  const [description, setDescription] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const [submitError, setSubmitError] = useState("");
  const [errors, setErrors] = useState<{ plate?: string; proof?: string }>({});
  const [busy, setBusy] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const hint = useMemo(() => plateHint(plate), [plate]);

  const pickFile = useCallback((next: File | undefined | null) => {
    if (!next) return;
    if (next.size > MAX_BYTES) {
      setErrors((e) => ({ ...e, proof: "That file is over 5 MB. Please choose a smaller one." }));
      return;
    }
    setErrors((e) => ({ ...e, proof: "" }));
    setFile(next);
    setPreview((old) => {
      if (old) URL.revokeObjectURL(old);
      return next.type.startsWith("image/") ? URL.createObjectURL(next) : null;
    });
  }, []);

  const clearFile = useCallback(() => {
    setFile(null);
    setPreview((old) => {
      if (old) URL.revokeObjectURL(old);
      return null;
    });
    if (fileRef.current) fileRef.current.value = "";
  }, []);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitError("");
    setErrors({});

    const clean = plate.replace(/[^A-Za-z0-9]/g, "");
    if (clean.length !== 10) {
      setErrors({ plate: "Enter the 10-character registration, e.g. MH12AB1234." });
      return;
    }

    setBusy(true);
    try {
      const body = new FormData();
      body.append("plate", clean);
      body.append("complaint_type", type);
      body.append("description", description);
      if (file) body.append("proof", file);
      const { data } = await api.post("/complaints", body, {
        headers: { "Content-Type": "multipart/form-data" },
      });
      nav(`/complaints/${data.id}`);
    } catch (err: any) {
      const detail = err.response?.data?.detail;
      setSubmitError(typeof detail === "string" ? detail : "Submission failed. Please try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto max-w-2xl">
      <PageHeader
        title="File a complaint"
        subtitle="Report a stolen or missing vehicle. Police verify every complaint before it enters the hotlist."
      />

      <Card className="mt-6">
        <form onSubmit={submit} className="space-y-6" noValidate>
          <Input
            label="Vehicle number"
            required
            value={plate}
            onChange={(e) => setPlate(e.target.value.toUpperCase().slice(0, 12))}
            placeholder="MH12AB1234"
            error={errors.plate}
            hint={hint}
            className="font-mono uppercase tracking-widest"
            spellCheck={false}
            autoCapitalize="characters"
          />

          <Select
            label="Complaint type"
            required
            value={type}
            onChange={(e) => setType(e.target.value)}
            options={TYPES}
            hint="Choose the option that best matches what happened."
          />

          <div className="space-y-1.5">
            <label htmlFor="description" className="block text-sm font-medium text-surface-text">
              Description
            </label>
            <textarea
              id="description"
              rows={4}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="When and where was the vehicle last seen? Anything that helps police confirm the report."
              className="w-full rounded-lg border border-surface-border bg-surface-card px-3 py-2 text-sm text-surface-text placeholder:text-surface-subtle focus:border-primary-600 focus:outline-none focus:ring-2 focus:ring-primary-600/25"
            />
            <p className="text-xs text-surface-muted">
              Location is never taken from this form - scanners report sightings automatically.
            </p>
          </div>

          <div className="space-y-1.5">
            <span className="block text-sm font-medium text-surface-text">Proof (optional)</span>
            <div
              onDragOver={(e: DragEvent) => {
                e.preventDefault();
                setDragging(true);
              }}
              onDragLeave={() => setDragging(false)}
              onDrop={(e: DragEvent) => {
                e.preventDefault();
                setDragging(false);
                pickFile(e.dataTransfer.files?.[0]);
              }}
              className={[
                "rounded-xl border-2 border-dashed p-6 text-center transition-colors",
                dragging ? "border-primary-500 bg-primary-50" : "border-surface-border bg-surface-raised",
              ].join(" ")}
            >
              {file ? (
                <div className="flex items-center gap-4 text-left">
                  {preview ? (
                    <img
                      src={preview}
                      alt="Selected proof preview"
                      className="h-16 w-20 shrink-0 rounded-lg border border-surface-border object-cover"
                      loading="lazy"
                    />
                  ) : (
                    <span className="flex h-16 w-20 shrink-0 items-center justify-center rounded-lg bg-surface-card text-primary-600">
                      <FileImage className="h-6 w-6" aria-hidden />
                    </span>
                  )}
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-medium text-surface-text">{file.name}</p>
                    <p className="text-xs text-surface-muted">{(file.size / 1024).toFixed(0)} KB</p>
                  </div>
                  <Button
                    variant="ghost"
                    size="sm"
                    icon={<Trash2 className="h-4 w-4" aria-hidden />}
                    onClick={clearFile}
                  >
                    Remove
                  </Button>
                </div>
              ) : (
                <>
                  <UploadCloud className="mx-auto h-6 w-6 text-surface-muted" aria-hidden />
                  <p className="mt-2 text-sm text-surface-muted">Drag an image or PDF here, or</p>
                  <Button
                    variant="secondary"
                    size="sm"
                    className="mt-3"
                    icon={<Upload className="h-4 w-4" aria-hidden />}
                    onClick={() => fileRef.current?.click()}
                  >
                    Choose file
                  </Button>
                  <p className="mt-2 text-xs text-surface-muted">JPG, PNG or PDF, up to 5 MB</p>
                </>
              )}
              <input
                ref={fileRef}
                type="file"
                accept={ACCEPT}
                className="sr-only"
                onChange={(e: ChangeEvent<HTMLInputElement>) => pickFile(e.target.files?.[0])}
              />
            </div>
            {errors.proof ? (
              <p role="alert" className="text-xs font-medium text-danger">
                {errors.proof}
              </p>
            ) : null}
          </div>

          {submitError ? (
            <ErrorState title="Could not file the complaint" description={submitError} className="py-4" />
          ) : null}

          <Button type="submit" size="lg" fullWidth loading={busy}>
            {busy ? "Submitting…" : "Submit complaint"}
          </Button>
        </form>
      </Card>
    </div>
  );
}
