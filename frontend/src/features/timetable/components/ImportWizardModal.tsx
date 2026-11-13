import React, { useEffect, useMemo, useReducer, useRef, useState } from "react";
import { Download, FileUp, Trash2 } from "lucide-react";
import { Modal } from "@/components/ui/Modal";
import { Button } from "@/components/ui/Button";
import { Alert } from "@/components/ui/Alert";
import { Input } from "@/components/ui/Input";
import { Select } from "@/components/ui/Select";
import { Table, type Column } from "@/components/ui/Table";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { Spinner } from "@/components/ui/Spinner";
import { getFriendlyError, parseApiError } from "@/api/errorMessages";
import { timetableApi, type TimetableItem } from "../api/timetableApi";
import type { ImportPreviewResponse } from "../types";
import { WEEKDAYS } from "../utils/timetableHelpers";
import {
  buildSaveEntries,
  bulkMap,
  canSaveDraft,
  initialSaveFlow,
  isClientTimeout,
  rowIssues,
  sanitizeDisplayFilename,
  saveFlowReducer,
  summarize,
  toWizardRows,
  truncateText,
  unknownNames,
  validateClientFile,
  type IssueCode,
  type MapKind,
  type PeriodTimes,
  type WizardRow,
} from "../utils/importWizard";

interface ImportWizardModalProps {
  isOpen: boolean;
  classId: string;
  className: string;
  onClose: () => void;
  /** Called with the saved draft so the editor can show it. */
  onSaved: (draft: TimetableItem) => void;
}

const STEPS = ["Template", "Upload", "Review", "Save"] as const;

const ISSUE_TEXT: Record<IssueCode, string> = {
  weekday: "Pick a weekday",
  time: "Enter start and end times",
  time_order: "End must be after start",
  period_time: "Enter the period times above",
  subject: "Pick a subject",
  teacher: "Pick a teacher or choose none",
};

/**
 * Four-step import wizard. The file and parsed rows live only in this component's
 * memory. The parent mounts it only while open, so closing discards everything.
 */
export const ImportWizardModal: React.FC<ImportWizardModalProps> = ({
  isOpen,
  classId,
  className,
  onClose,
  onSaved,
}) => {
  if (!isOpen) return null;
  return (
    <WizardBody classId={classId} className={className} onClose={onClose} onSaved={onSaved} />
  );
};

const WizardBody: React.FC<Omit<ImportWizardModalProps, "isOpen">> = ({
  classId,
  className,
  onClose,
  onSaved,
}) => {
  const [step, setStep] = useState<number>(0);
  const [downloading, setDownloading] = useState<"csv" | "xlsx" | null>(null);
  const [parsing, setParsing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [retryLeft, setRetryLeft] = useState(0);
  const [fileLabel, setFileLabel] = useState<string>("");
  const [preview, setPreview] = useState<ImportPreviewResponse | null>(null);
  const [rows, setRows] = useState<WizardRow[]>([]);
  const [periodTimes, setPeriodTimes] = useState<PeriodTimes>({});
  const [confirmDiscard, setConfirmDiscard] = useState(false);
  const [flow, dispatch] = useReducer(saveFlowReducer, initialSaveFlow);
  const fileInput = useRef<HTMLInputElement>(null);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const ctx = useMemo(
    () => ({
      subjectIds: new Set((preview?.available_subjects ?? []).map((s) => s.id)),
      teacherIds: new Set((preview?.available_teachers ?? []).map((t) => t.id)),
    }),
    [preview]
  );
  const summary = useMemo(() => summarize(rows, periodTimes, ctx), [rows, periodTimes, ctx]);
  const saveOk = canSaveDraft(rows, periodTimes, ctx);
  const dirty = preview !== null && flow.phase !== "saved";
  const periodLabels = useMemo(
    () => [...new Set(rows.filter((r) => r.needsPeriodTime && r.periodLabel).map((r) => r.periodLabel as string))].sort(),
    [rows]
  );

  // Warn before the tab closes while unsaved rows exist.
  useEffect(() => {
    if (!dirty) return;
    const handler = (e: BeforeUnloadEvent) => {
      e.preventDefault();
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [dirty]);

  // Count down the Retry-After hint so the user knows when to try again.
  useEffect(() => {
    if (retryLeft <= 0) return;
    const id = window.setTimeout(() => setRetryLeft((n) => n - 1), 1000);
    return () => window.clearTimeout(id);
  }, [retryLeft]);

  const requestClose = () => {
    if (parsing || flow.phase === "saving") return;
    if (dirty) setConfirmDiscard(true);
    else onClose();
  };

  const messageFor = (err: unknown, fallback: string): string =>
    isClientTimeout(err)
      ? "This took too long. Please try again."
      : getFriendlyError(err, fallback);

  // ---- step 1: template ----
  const handleDownload = async (format: "csv" | "xlsx") => {
    setError(null);
    setDownloading(format);
    let url: string | null = null;
    try {
      const blob = await timetableApi.downloadTemplate(classId, format);
      url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `timetable-template.${format}`;
      document.body.appendChild(a);
      a.click();
      a.remove();
    } catch (err) {
      setError(messageFor(err, "Could not download the template."));
    } finally {
      if (url) URL.revokeObjectURL(url);
      if (alive.current) setDownloading(null);
    }
  };

  // ---- step 2: upload ----
  const handleFile = async (file: File | undefined) => {
    if (!file) return;
    setError(null);
    const clientProblem = validateClientFile(file.name, file.size);
    if (clientProblem) {
      setError(clientProblem);
      return;
    }
    setParsing(true);
    setFileLabel(sanitizeDisplayFilename(file.name));
    try {
      const res = await timetableApi.previewImport(classId, file);
      if (!alive.current) return;
      setPreview(res);
      setRows(toWizardRows(res.rows));
      setPeriodTimes({});
      dispatch({ type: "reset" });
      setStep(2);
    } catch (err) {
      if (!alive.current) return;
      setError(messageFor(err, "We could not read this file."));
      const p = parseApiError(err);
      if (p.status === 429 && p.retryAfterSeconds) setRetryLeft(p.retryAfterSeconds);
    } finally {
      if (fileInput.current) fileInput.current.value = "";
      if (alive.current) setParsing(false);
    }
  };

  // ---- step 3: editing helpers ----
  const patchRow = (key: string, patch: Partial<WizardRow>) =>
    setRows((prev) => prev.map((r) => (r.key === key ? { ...r, ...patch } : r)));
  const removeRow = (key: string) => setRows((prev) => prev.filter((r) => r.key !== key));
  const setPeriod = (label: string, part: "start" | "end", value: string) =>
    setPeriodTimes((prev) => ({
      ...prev,
      [label]: { start: prev[label]?.start ?? "", end: prev[label]?.end ?? "", [part]: value },
    }));

  const subjectOptions = (preview?.available_subjects ?? []).map((s) => ({
    value: s.id,
    label: s.code ? `${truncateText(s.name, 40)} (${truncateText(s.code, 12)})` : truncateText(s.name, 40),
  }));
  const teacherOptions = (preview?.available_teachers ?? []).map((t) => ({
    value: t.id,
    label: truncateText(t.full_name, 50),
  }));

  const columns: Column<WizardRow>[] = [
    {
      key: "status",
      header: "Status",
      render: (r) => {
        const issues = rowIssues(r, periodTimes, ctx);
        return issues.length === 0 ? (
          <span className="text-xs font-medium text-success-text">OK</span>
        ) : (
          <span className="text-xs text-danger-text">{issues.map((i) => ISSUE_TEXT[i]).join(". ")}</span>
        );
      },
    },
    {
      key: "day",
      header: "Weekday",
      render: (r) => (
        <Select
          aria-label="Weekday"
          value={r.day_of_week === null ? "" : String(r.day_of_week)}
          placeholder="Select"
          onChange={(e) =>
            patchRow(r.key, { day_of_week: e.target.value === "" ? null : Number(e.target.value) })
          }
          options={WEEKDAYS.map((d) => ({ value: String(d.index), label: d.label }))}
        />
      ),
    },
    {
      key: "times",
      header: "Time",
      render: (r) =>
        r.needsPeriodTime ? (
          <span className="text-xs text-text-muted">{truncateText(r.periodLabel, 30)} (see period times)</span>
        ) : (
          <div className="flex items-center gap-1.5">
            <Input
              aria-label="Start time"
              type="time"
              value={r.start_time}
              onChange={(e) => patchRow(r.key, { start_time: e.target.value })}
            />
            <Input
              aria-label="End time"
              type="time"
              value={r.end_time}
              onChange={(e) => patchRow(r.key, { end_time: e.target.value })}
            />
          </div>
        ),
    },
    {
      key: "subject",
      header: "Subject",
      render: (r) => (
        <div className="space-y-1">
          <Select
            aria-label="Subject"
            value={r.subject_id ?? ""}
            placeholder="Select subject"
            onChange={(e) => patchRow(r.key, { subject_id: e.target.value || null })}
            options={subjectOptions}
          />
          {r.rawSubject && !r.subject_id && (
            <p className="text-xs text-text-muted">File said: {truncateText(r.rawSubject, 40)}</p>
          )}
        </div>
      ),
    },
    {
      key: "teacher",
      header: "Teacher",
      render: (r) => (
        <div className="space-y-1">
          <Select
            aria-label="Teacher"
            value={r.teacher_id ?? ""}
            placeholder={r.teacherTouched || !r.rawTeacher ? "No teacher" : "Select teacher"}
            onChange={(e) =>
              patchRow(r.key, { teacher_id: e.target.value || null, teacherTouched: true })
            }
            options={teacherOptions}
          />
          {r.rawTeacher && !r.teacher_id && (
            <p className="text-xs text-text-muted">File said: {truncateText(r.rawTeacher, 40)}</p>
          )}
        </div>
      ),
    },
    {
      key: "room",
      header: "Room",
      render: (r) => (
        <Input
          aria-label="Room"
          value={r.room}
          maxLength={50}
          onChange={(e) => patchRow(r.key, { room: e.target.value })}
        />
      ),
    },
    {
      key: "note",
      header: "Note",
      render: (r) => (
        <Input
          aria-label="Note"
          value={r.note}
          maxLength={200}
          onChange={(e) => patchRow(r.key, { note: e.target.value })}
        />
      ),
    },
    {
      key: "actions",
      header: "Actions",
      render: (r) => (
        <Button
          variant="ghost"
          size="sm"
          aria-label="Delete row"
          className="min-h-[44px]"
          onClick={() => removeRow(r.key)}
        >
          <Trash2 className="w-4 h-4 mr-1" />
          Delete
        </Button>
      ),
    },
  ];

  // ---- step 4: save ----
  const runSave = async (replace: boolean) => {
    const entries = buildSaveEntries(rows, periodTimes);
    try {
      const draft = await timetableApi.saveImportedDraft(classId, entries, replace);
      if (!alive.current) return;
      dispatch({ type: "success" });
      onSaved(draft);
    } catch (err) {
      if (!alive.current) return;
      if (!replace && parseApiError(err).status === 409) {
        let oldCount: number | null = null;
        try {
          oldCount = (await timetableApi.getDraftTimetable(classId)).draft?.entries.length ?? null;
        } catch {
          oldCount = null;
        }
        if (alive.current) dispatch({ type: "conflict", oldCount, newCount: entries.length });
        return;
      }
      dispatch({ type: "failure", message: messageFor(err, "Could not save the draft.") });
    }
  };

  const handleSave = () => {
    if (!saveOk) return;
    dispatch({ type: "save" });
    void runSave(false);
  };
  const handleConfirmReplace = () => {
    dispatch({ type: "confirm_replace" });
    void runSave(true);
  };

  const renderMapping = (kind: MapKind) => {
    const names = unknownNames(rows, kind, ctx);
    if (names.length === 0) return null;
    const options = kind === "subject" ? subjectOptions : teacherOptions;
    return (
      <div className="space-y-2">
        <h4 className="text-sm font-semibold text-text">
          Unrecognized {kind === "subject" ? "subjects" : "teachers"}
        </h4>
        {names.map((n) => (
          <div key={n.name} className="flex flex-col sm:flex-row sm:items-center gap-2">
            <span className="text-sm text-text sm:w-1/2 break-words">
              {truncateText(n.name, 50)} ({n.count} row{n.count === 1 ? "" : "s"})
            </span>
            <div className="sm:w-1/2">
              <Select
                aria-label={`Map ${truncateText(n.name, 30)} to an existing ${kind}`}
                value=""
                placeholder={`Map to existing ${kind}`}
                onChange={(e) => setRows((prev) => bulkMap(prev, kind, n.name, e.target.value, ctx))}
                options={options}
              />
            </div>
          </div>
        ))}
      </div>
    );
  };

  const stepper = (
    <ol className="flex items-center gap-2 text-xs flex-wrap" aria-label="Import steps">
      {STEPS.map((label, i) => (
        <li
          key={label}
          aria-current={i === step ? "step" : undefined}
          className={
            i === step
              ? "px-2.5 py-1 rounded-full bg-primary text-primary-foreground font-semibold"
              : "px-2.5 py-1 rounded-full border border-border text-text-muted"
          }
        >
          {i + 1}. {label}
        </li>
      ))}
    </ol>
  );

  const footer = (
    <div className="flex flex-col-reverse sm:flex-row sm:justify-between gap-2.5 w-full">
      <Button variant="outline" className="min-h-[44px]" onClick={requestClose} disabled={parsing || flow.phase === "saving"}>
        Cancel
      </Button>
      <div className="flex flex-col-reverse sm:flex-row gap-2.5">
        {step === 1 && (
          <Button variant="outline" className="min-h-[44px]" onClick={() => setStep(0)} disabled={parsing}>
            Back
          </Button>
        )}
        {step === 2 && (
          <Button variant="outline" className="min-h-[44px]" onClick={() => setStep(1)}>
            Upload another file
          </Button>
        )}
        {step === 3 && (
          <Button variant="outline" className="min-h-[44px]" onClick={() => setStep(2)} disabled={flow.phase === "saving"}>
            Back to review
          </Button>
        )}
        {step === 0 && (
          <Button className="min-h-[44px]" onClick={() => setStep(1)}>
            Next: upload
          </Button>
        )}
        {step === 2 && (
          <Button className="min-h-[44px]" onClick={() => setStep(3)} disabled={!saveOk}>
            Next: save
          </Button>
        )}
        {step === 3 && (
          <Button
            className="min-h-[44px]"
            onClick={handleSave}
            disabled={!saveOk}
            isLoading={flow.phase === "saving"}
          >
            Save as draft
          </Button>
        )}
      </div>
    </div>
  );

  return (
    <>
      <Modal
        isOpen
        onClose={requestClose}
        closeOnBackdrop={false}
        size="full"
        title="Import timetable"
        description={`Importing into ${truncateText(className, 60)}. Nothing is published by this import.`}
        footer={footer}
      >
        <div className="space-y-4">
          {stepper}
          {error && (
            <Alert variant="danger" onClose={() => setError(null)}>
              {error}
              {retryLeft > 0 ? ` (${retryLeft}s)` : ""}
            </Alert>
          )}

          {step === 0 && (
            <div className="space-y-3">
              <p className="text-sm text-text-muted">
                Download the template, fill it in, then upload it. It already lists this class&apos;s subjects and teachers.
              </p>
              <div className="flex flex-col sm:flex-row gap-2.5">
                <Button variant="outline" className="min-h-[44px]" isLoading={downloading === "csv"} disabled={downloading !== null} onClick={() => handleDownload("csv")}>
                  <Download className="w-4 h-4 mr-1.5" />
                  CSV template
                </Button>
                <Button variant="outline" className="min-h-[44px]" isLoading={downloading === "xlsx"} disabled={downloading !== null} onClick={() => handleDownload("xlsx")}>
                  <Download className="w-4 h-4 mr-1.5" />
                  XLSX template
                </Button>
              </div>
            </div>
          )}

          {step === 1 && (
            <div className="space-y-3">
              <p className="text-sm text-text-muted">
                Choose a CSV, XLSX, DOCX or text PDF file (max 2 MB). The file is read for review only and is not stored.
              </p>
              <input
                ref={fileInput}
                type="file"
                className="hidden"
                accept=".csv,.xlsx,.docx,.pdf"
                onChange={(e) => void handleFile(e.target.files?.[0])}
              />
              {parsing ? (
                <div className="flex items-center gap-2 text-sm text-text-muted" role="status">
                  <Spinner />
                  <span>Reading {fileLabel}…</span>
                </div>
              ) : (
                <Button
                  className="min-h-[44px]"
                  disabled={retryLeft > 0}
                  onClick={() => fileInput.current?.click()}
                >
                  <FileUp className="w-4 h-4 mr-1.5" />
                  Choose file
                </Button>
              )}
            </div>
          )}

          {step === 2 && preview && (
            <div className="space-y-4">
              <div className="flex flex-wrap gap-x-5 gap-y-1 p-3 rounded-lg border border-border bg-surface-hover text-sm" role="status">
                <span className="text-text-muted">File: {fileLabel}</span>
                <span className="text-text">Total: {summary.total}</span>
                <span className="text-success-text">Valid: {summary.valid}</span>
                <span className={summary.needsAttention ? "text-danger-text" : "text-text-muted"}>
                  Needs attention: {summary.needsAttention}
                </span>
              </div>
              {rows.length > 200 && (
                <Alert variant="warning">A timetable can have at most 200 sessions. Delete some rows to continue.</Alert>
              )}
              {periodLabels.length > 0 && (
                <div className="space-y-2">
                  <h4 className="text-sm font-semibold text-text">Period times</h4>
                  <p className="text-xs text-text-muted">The file uses period names. Enter when each period starts and ends.</p>
                  {periodLabels.map((label) => (
                    <div key={label} className="flex flex-col sm:flex-row sm:items-center gap-2">
                      <span className="text-sm text-text sm:w-1/3 break-words">{truncateText(label, 30)}</span>
                      <Input
                        aria-label={`${truncateText(label, 30)} start time`}
                        type="time"
                        value={periodTimes[label]?.start ?? ""}
                        onChange={(e) => setPeriod(label, "start", e.target.value)}
                      />
                      <Input
                        aria-label={`${truncateText(label, 30)} end time`}
                        type="time"
                        value={periodTimes[label]?.end ?? ""}
                        onChange={(e) => setPeriod(label, "end", e.target.value)}
                      />
                    </div>
                  ))}
                </div>
              )}
              {renderMapping("subject")}
              {renderMapping("teacher")}
              <Table
                columns={columns}
                data={rows}
                keyExtractor={(r) => r.key}
                emptyTitle="No rows left"
                emptyDescription="Upload another file to continue."
              />
            </div>
          )}

          {step === 3 && (
            <div className="space-y-3">
              <p className="text-sm text-text">
                {summary.total} session{summary.total === 1 ? "" : "s"} will be saved to the draft timetable for this class.
              </p>
              <Alert variant="info">Saving creates or updates a draft only. Nothing is published until you publish it.</Alert>
              {flow.phase === "error" && <Alert variant="danger">{flow.message}</Alert>}
            </div>
          )}
        </div>
      </Modal>

      <ConfirmDialog
        isOpen={flow.phase === "confirm_replace" || (flow.phase === "saving" && flow.replace)}
        onClose={() => dispatch({ type: "cancel_replace" })}
        onConfirm={handleConfirmReplace}
        title="Replace existing draft?"
        message={`This class already has a draft${
          flow.phase === "confirm_replace" && flow.oldCount !== null ? ` with ${flow.oldCount} sessions` : ""
        }. Replacing it will discard those sessions and use the ${summary.total} imported ones.`}
        confirmText="Replace draft"
        confirmVariant="danger"
        isLoading={flow.phase === "saving"}
      />

      <ConfirmDialog
        isOpen={confirmDiscard}
        onClose={() => setConfirmDiscard(false)}
        onConfirm={onClose}
        title="Discard import?"
        message="You have unsaved imported rows. If you close now they will be lost."
        confirmText="Discard"
        confirmVariant="danger"
      />
    </>
  );
};
