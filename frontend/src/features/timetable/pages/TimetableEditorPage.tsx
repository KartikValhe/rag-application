import React, { useEffect, useRef, useState, useCallback } from "react";
import { useSearchParams } from "react-router-dom";
import { AppShell, PageHeader } from "@/components/layout";
import {
  timetableApi,
  TimetableItem,
  TimetableEntryItem,
  ConflictItem,
  EntryFormPayload,
} from "../api/timetableApi";
import { WeeklyTimetableGrid } from "../components/WeeklyTimetableGrid";
import { EntryFormModal } from "../components/EntryFormModal";
import { ConflictWarningDialog } from "../components/ConflictWarningDialog";
import { conflictsFromPublishError, draftEditorState, nextPublishStep } from "../utils/timetableHelpers";
import { ImportWizardModal } from "../components/ImportWizardModal";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { Select } from "@/components/ui/Select";
import { Skeleton } from "@/components/ui/Skeleton";
import { getFriendlyError, parseApiError } from "@/api/errorMessages";
import { Can } from "@/components/auth/Can";
import { usePermission } from "@/hooks/usePermission";
import { useToast } from "@/components/ui/Toast";
import { academicApi, ClassItem } from "@/features/academic/api/academicApi";
import {
  Plus,
  Send,
  RotateCcw,
  School,
  AlertCircle,
  Eye,
  Edit3,
  Upload,
} from "lucide-react";

export const TimetableEditorPage: React.FC = () => {
  const { isAdmin } = usePermission();
  const toast = useToast();
  const [searchParams, setSearchParams] = useSearchParams();

  // Class selection state
  const [classes, setClasses] = useState<ClassItem[]>([]);
  const [selectedClassId, setSelectedClassId] = useState<string>("");
  const [isClassesLoading, setIsClassesLoading] = useState<boolean>(true);

  // Timetable state
  const [timetable, setTimetable] = useState<TimetableItem | null>(null);
  const [viewMode, setViewMode] = useState<"draft" | "published">("draft");
  const [isLoadingTimetable, setIsLoadingTimetable] = useState<boolean>(false);
  const [timetableError, setTimetableError] = useState<string | null>(null);
  // Version of the class's published timetable, or null. Decides whether "no draft" can offer a clone.
  const [publishedVersion, setPublishedVersion] = useState<number | null>(null);
  // "<classId>:<mode>" of the last finished load. Anything else means the state on screen is stale.
  const [loadedKey, setLoadedKey] = useState<string | null>(null);
  const loadedKeyRef = useRef<string | null>(null);
  const loadSeq = useRef(0); // only the newest load may write state
  const [isCloning, setIsCloning] = useState<boolean>(false);

  // Modal dialog states
  const [isEntryModalOpen, setIsEntryModalOpen] = useState<boolean>(false);
  const [editingEntry, setEditingEntry] = useState<TimetableEntryItem | null>(null);
  const [deletingEntry, setDeletingEntry] = useState<TimetableEntryItem | null>(null);
  const [isEntrySaving, setIsEntrySaving] = useState<boolean>(false);
  const [isEntryDeleting, setIsEntryDeleting] = useState<boolean>(false);

  const [isImportOpen, setIsImportOpen] = useState<boolean>(false);

  // Publish & Conflict warning states
  const [isConflictModalOpen, setIsConflictModalOpen] = useState<boolean>(false);
  const [detectedConflicts, setDetectedConflicts] = useState<ConflictItem[]>([]);
  const [isPublishing, setIsPublishing] = useState<boolean>(false);
  const [isUnpublishing, setIsUnpublishing] = useState<boolean>(false);

  // 1. Fetch available classes (all classes for admin, assigned classes for teacher)
  useEffect(() => {
    let isMounted = true;
    setIsClassesLoading(true);

    const loadClasses = isAdmin
      ? academicApi.listClasses({ page_size: 100 }).then((res) => res.items || [])
      : academicApi.getMyClasses().then((items) =>
          items.map((c) => ({
            id: c.id,
            name: c.name,
            kind: c.kind,
            created_at: "",
          }))
        );

    loadClasses
      .then((items) => {
        if (!isMounted) return;
        setClasses(items);
        const paramId = searchParams.get("classId");
        if (paramId && items.some((c) => c.id === paramId)) {
          setSelectedClassId(paramId);
        } else if (items.length > 0) {
          setSelectedClassId(items[0].id);
        }
      })
      .catch((err) => {
        if (!isMounted) return;
        toast.error(getFriendlyError(err, "Failed to load classes for timetable editing."));
      })
      .finally(() => {
        if (!isMounted) return;
        setIsClassesLoading(false);
      });

    return () => {
      isMounted = false;
    };
  }, [isAdmin]);

  // Sync selectedClassId to URL search params
  const handleSelectClass = (classId: string) => {
    setSelectedClassId(classId);
    setSearchParams(classId ? { classId } : {});
  };

  // 2. Fetch draft or published timetable for selected class
  const loadTimetable = useCallback(async () => {
    if (!selectedClassId) return;
    const key = `${selectedClassId}:${viewMode}`;
    const requestId = ++loadSeq.current;
    const isStale = () => requestId !== loadSeq.current;
    setIsLoadingTimetable(true);
    setTimetableError(null);
    if (loadedKeyRef.current !== key) {
      setTimetable(null);
      setLoadedKey(null);
      loadedKeyRef.current = null;
    }

    try {
      if (viewMode === "published") {
        const res = await timetableApi.getClassTimetable(selectedClassId);
        if (isStale()) return;
        setTimetable(res);
        if (!res) {
          setTimetableError("No published timetable exists for this class yet. Switch to Draft to edit and publish.");
        }
      } else {
        // "No draft" is a normal state (for example right after a publish), not an error.
        const res = await timetableApi.getDraftTimetable(selectedClassId);
        if (isStale()) return;
        setTimetable(res.draft);
        setPublishedVersion(res.published_version);
      }
      loadedKeyRef.current = key;
      setLoadedKey(key);
    } catch (err: unknown) {
      if (isStale()) return;
      setTimetable(null);
      if (viewMode === "published" && parseApiError(err).status === 404) {
        setTimetableError("No published timetable exists for this class yet. Switch to Draft to edit and publish.");
      } else {
        setTimetableError(getFriendlyError(err, "Failed to load timetable for this class."));
      }
    } finally {
      if (!isStale()) setIsLoadingTimetable(false);
    }
  }, [selectedClassId, viewMode]);

  useEffect(() => {
    loadTimetable();
  }, [loadTimetable]);

  // Start editing again: copy the published timetable into a new draft.
  const handleStartEditing = async () => {
    if (!selectedClassId) return;
    setIsCloning(true);
    try {
      const draft = await timetableApi.cloneDraft(selectedClassId);
      setTimetable(draft);
      toast.success("Draft created from the published timetable.");
    } catch (err: unknown) {
      toast.error(getFriendlyError(err, "Failed to start editing."));
      await loadTimetable();
    } finally {
      setIsCloning(false);
    }
  };

  // 3. Entry CRUD Handlers
  const handleOpenAddEntry = (defaultDay?: number) => {
    setEditingEntry(
      defaultDay !== undefined
        ? ({
            id: "",
            timetable_id: "",
            class_id: selectedClassId,
            subject_id: "",
            subject_name: "",
            day_of_week: defaultDay,
            day_name: "",
            start_time: "09:00",
            end_time: "10:00",
            created_at: "",
          } as TimetableEntryItem)
        : null
    );
    setIsEntryModalOpen(true);
  };

  const handleOpenEditEntry = (entry: TimetableEntryItem) => {
    setEditingEntry(entry);
    setIsEntryModalOpen(true);
  };

  const handleSaveEntry = async (payload: EntryFormPayload) => {
    if (!selectedClassId) return;
    setIsEntrySaving(true);
    try {
      if (editingEntry?.id) {
        await timetableApi.updateDraftEntry(selectedClassId, editingEntry.id, payload);
        toast.success("Lecture updated successfully.");
      } else {
        await timetableApi.addDraftEntry(selectedClassId, payload);
        toast.success("Lecture added to draft timetable.");
      }
      await loadTimetable();
    } finally {
      setIsEntrySaving(false);
    }
  };

  const handleDeleteEntry = async () => {
    if (!selectedClassId || !deletingEntry) return;
    setIsEntryDeleting(true);
    try {
      await timetableApi.deleteDraftEntry(selectedClassId, deletingEntry.id);
      toast.success("Lecture removed from draft.");
      setDeletingEntry(null);
      await loadTimetable();
    } catch (err: unknown) {
      toast.error(getFriendlyError(err, "Failed to delete lecture entry."));
    } finally {
      setIsEntryDeleting(false);
    }
  };

  // 4. Conflict Check & Publish Flow
  const handleInitiatePublish = async () => {
    if (!selectedClassId) return;
    setIsPublishing(true);
    try {
      const checkRes = await timetableApi.checkConflicts(selectedClassId);
      if (nextPublishStep(checkRes) === "confirm") {
        setDetectedConflicts(checkRes.conflicts);
        setIsConflictModalOpen(true);
        return;
      }
      // No conflicts -> publish directly
      await timetableApi.publishTimetable(selectedClassId, false);
      toast.success("Timetable published successfully! Enrolled students can now view it.");
      setViewMode("published"); // the effect reloads for the new mode
    } catch (err: unknown) {
      // A conflict created between the check and the publish comes back as a 409.
      const conflicts = conflictsFromPublishError(err);
      if (conflicts) {
        setDetectedConflicts(conflicts);
        setIsConflictModalOpen(true);
      } else {
        toast.error(getFriendlyError(err, "Failed to publish timetable."));
      }
    } finally {
      setIsPublishing(false);
    }
  };

  const handleConfirmOverridePublish = async () => {
    if (!selectedClassId) return;
    setIsPublishing(true);
    try {
      await timetableApi.publishTimetable(selectedClassId, true);
      toast.success("Timetable published with conflicts overridden.");
      setIsConflictModalOpen(false);
      setViewMode("published"); // the effect reloads for the new mode
    } catch (err: unknown) {
      toast.error(getFriendlyError(err, "Failed to publish timetable."));
    } finally {
      setIsPublishing(false);
    }
  };

  // 5. Unpublish Handler
  const handleUnpublish = async () => {
    if (!selectedClassId) return;
    setIsUnpublishing(true);
    try {
      await timetableApi.unpublishTimetable(selectedClassId);
      toast.info("Timetable reverted to draft.");
      setViewMode("draft"); // the effect reloads for the new mode
    } catch (err: unknown) {
      toast.error(getFriendlyError(err, "Failed to unpublish timetable."));
    } finally {
      setIsUnpublishing(false);
    }
  };

  const editorState = draftEditorState({
    loaded: loadedKey === `${selectedClassId}:${viewMode}` && !timetableError,
    hasDraft: viewMode === "draft" && timetable !== null,
    publishedVersion,
  });
  const selectedClass = classes.find((c) => c.id === selectedClassId);

  return (
    <AppShell tenantName="Academic Workspace">
      <PageHeader
        title="Timetable Editor"
        description="Schedule weekly lectures, manage drafts, inspect schedule overlaps, and publish official class timetables."
        breadcrumbs={[
          { label: "Administration", href: "/admin" },
          { label: "Timetables" },
        ]}
        actions={
          <div className="flex items-center gap-2 flex-wrap">
            {timetable && viewMode === "draft" && (
              <Can permission="timetable:publish">
                <Button
                  variant="primary"
                  onClick={handleInitiatePublish}
                  isLoading={isPublishing}
                  disabled={isLoadingTimetable || timetable.entries.length === 0}
                  className="min-h-[44px]"
                >
                  <Send className="w-4 h-4 mr-1.5" />
                  Publish Timetable
                </Button>
              </Can>
            )}

            {timetable?.status === "published" && (
              <Can permission="timetable:publish">
                <Button
                  variant="outline"
                  onClick={handleUnpublish}
                  isLoading={isUnpublishing}
                  disabled={isLoadingTimetable}
                  className="min-h-[44px]"
                >
                  <RotateCcw className="w-4 h-4 mr-1.5" />
                  Unpublish to Draft
                </Button>
              </Can>
            )}
          </div>
        }
      />

      {/* Class Selector Bar & Mode Switcher */}
      <div className="p-4 rounded-lg border border-border bg-surface mb-6 space-y-4">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
          <div className="flex-1 max-w-md">
            <label className="block text-xs font-semibold text-text-muted uppercase tracking-wider mb-1.5">
              Select Class Cohort
            </label>
            {isClassesLoading ? (
              <Skeleton className="h-10 w-full" />
            ) : classes.length === 0 ? (
              <p className="text-xs text-text-muted">No classes available.</p>
            ) : (
              <Select
                value={selectedClassId}
                onChange={(e) => handleSelectClass(e.target.value)}
                options={classes.map((c) => ({
                  value: c.id,
                  label: `${c.name} (${c.kind})`,
                }))}
                className="min-h-[44px]"
              />
            )}
          </div>

          {/* Mode Switcher Tabs */}
          <div className="flex items-center gap-1.5 self-start sm:self-end">
            <button
              type="button"
              onClick={() => setViewMode("draft")}
              className={`min-h-[44px] px-3.5 py-2 rounded-lg text-xs font-semibold flex items-center gap-1.5 border transition-colors ${
                viewMode === "draft"
                  ? "bg-primary text-primary-foreground border-primary"
                  : "bg-surface text-text-muted border-border hover:bg-surface-hover"
              }`}
            >
              <Edit3 className="w-3.5 h-3.5" />
              <span>Draft Editor</span>
              {timetable?.status === "draft" && (
                <span className="w-2 h-2 rounded-full bg-warning inline-block ml-1" />
              )}
            </button>
            <button
              type="button"
              onClick={() => setViewMode("published")}
              className={`min-h-[44px] px-3.5 py-2 rounded-lg text-xs font-semibold flex items-center gap-1.5 border transition-colors ${
                viewMode === "published"
                  ? "bg-primary text-primary-foreground border-primary"
                  : "bg-surface text-text-muted border-border hover:bg-surface-hover"
              }`}
            >
              <Eye className="w-3.5 h-3.5" />
              <span>Published View</span>
            </button>
          </div>
        </div>

        {/* Timetable Metadata Bar */}
        {selectedClass && (
          <div className="flex items-center justify-between pt-3 border-t border-border/50 text-xs flex-wrap gap-2">
            <div className="flex items-center gap-3">
              <span className="text-text font-medium flex items-center gap-1.5">
                <School className="w-4 h-4 text-primary" />
                {selectedClass.name}
              </span>
              {timetable && (
                <Badge
                  variant={timetable.status === "published" ? "success" : "warning"}
                  size="sm"
                >
                  {timetable.status === "published" ? `Published (v${timetable.version})` : "Draft In Progress"}
                </Badge>
              )}
            </div>

            {viewMode === "draft" && (
              <div className="flex items-center gap-2 flex-wrap">
                {(editorState === "editing" || editorState === "start_blank") && (
                  <Can permission="timetable:manage">
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => setIsImportOpen(true)}
                      className="min-h-[44px]"
                    >
                      <Upload className="w-3.5 h-3.5 mr-1" />
                      Import
                    </Button>
                  </Can>
                )}
                {(editorState === "editing" || editorState === "start_blank") && (
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => handleOpenAddEntry()}
                    className="min-h-[44px]"
                  >
                    <Plus className="w-3.5 h-3.5 mr-1" />
                    Add Lecture
                  </Button>
                )}
              </div>
            )}
          </div>
        )}
      </div>

      {/* Main Timetable Content */}
      {timetableError ? (
        <div className="p-6 rounded-lg border border-border bg-surface text-center space-y-3">
          <AlertCircle className="w-8 h-8 text-warning mx-auto opacity-70" />
          <h4 className="text-base font-semibold text-text">No Timetable Available</h4>
          <p className="text-xs text-text-muted max-w-md mx-auto">{timetableError}</p>
          {viewMode === "published" && (
            <Button
              variant="primary"
              size="sm"
              onClick={() => setViewMode("draft")}
              className="min-h-[44px]"
            >
              Switch to Draft Editor
            </Button>
          )}
        </div>
      ) : editorState === "start_from_published" || editorState === "start_blank" ? (
        <div className="p-6 rounded-lg border border-border bg-surface text-center space-y-3">
          <Edit3 className="w-8 h-8 text-primary mx-auto opacity-70" />
          {editorState === "start_from_published" ? (
            <>
              <h4 className="text-base font-semibold text-text">
                No draft. Start editing from the published timetable
              </h4>
              <p className="text-xs text-text-muted max-w-md mx-auto">
                The published timetable (v{publishedVersion}) stays visible to students until you publish a new draft.
              </p>
              <Can permission="timetable:manage">
                <Button
                  variant="primary"
                  size="sm"
                  onClick={handleStartEditing}
                  isLoading={isCloning}
                  className="min-h-[44px]"
                >
                  Start editing
                </Button>
              </Can>
            </>
          ) : (
            <>
              <h4 className="text-base font-semibold text-text">No draft yet</h4>
              <p className="text-xs text-text-muted max-w-md mx-auto">
                Add a lecture or import a file to start a draft for this class.
              </p>
            </>
          )}
        </div>
      ) : (
        <WeeklyTimetableGrid
          entries={timetable?.entries || []}
          isLoading={isLoadingTimetable}
          canEdit={editorState === "editing"}
          onAddEntry={handleOpenAddEntry}
          onEditEntry={handleOpenEditEntry}
          onDeleteEntry={(entry) => setDeletingEntry(entry)}
        />
      )}

      {/* Add / Edit Entry Modal */}
      {selectedClassId && (
        <EntryFormModal
          isOpen={isEntryModalOpen}
          onClose={() => setIsEntryModalOpen(false)}
          classId={selectedClassId}
          initialData={editingEntry}
          onSave={handleSaveEntry}
          isSaving={isEntrySaving}
        />
      )}

      {selectedClassId && (
        <ImportWizardModal
          isOpen={isImportOpen}
          classId={selectedClassId}
          className={selectedClass?.name ?? ""}
          onClose={() => setIsImportOpen(false)}
          onSaved={(draft) => {
            setIsImportOpen(false);
            setViewMode("draft");
            setTimetable(draft);
            toast.success("Draft saved from import. Nothing is published yet.");
          }}
        />
      )}

      {/* Confirm Delete Dialog */}
      <ConfirmDialog
        isOpen={Boolean(deletingEntry)}
        onClose={() => setDeletingEntry(null)}
        onConfirm={handleDeleteEntry}
        title="Delete Lecture Session"
        message={`Are you sure you want to delete ${deletingEntry?.subject_name || "this session"} on ${deletingEntry?.day_name}? This change will be saved to your draft.`}
        confirmText="Delete Session"
        confirmVariant="danger"
        isLoading={isEntryDeleting}
      />

      {/* Conflict Warnings Dialog */}
      <ConflictWarningDialog
        isOpen={isConflictModalOpen}
        onClose={() => setIsConflictModalOpen(false)}
        conflicts={detectedConflicts}
        onConfirmPublish={handleConfirmOverridePublish}
        isPublishing={isPublishing}
      />
    </AppShell>
  );
};
