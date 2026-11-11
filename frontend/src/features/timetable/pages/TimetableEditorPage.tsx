import React, { useEffect, useState, useCallback } from "react";
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
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { Select } from "@/components/ui/Select";
import { Skeleton } from "@/components/ui/Skeleton";
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

  // Modal dialog states
  const [isEntryModalOpen, setIsEntryModalOpen] = useState<boolean>(false);
  const [editingEntry, setEditingEntry] = useState<TimetableEntryItem | null>(null);
  const [deletingEntry, setDeletingEntry] = useState<TimetableEntryItem | null>(null);
  const [isEntrySaving, setIsEntrySaving] = useState<boolean>(false);
  const [isEntryDeleting, setIsEntryDeleting] = useState<boolean>(false);

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
        toast.error(
          err.response?.data?.detail?.message || "Failed to load classes for timetable editing."
        );
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
    setIsLoadingTimetable(true);
    setTimetableError(null);

    try {
      if (viewMode === "published") {
        const res = await timetableApi.getClassTimetable(selectedClassId);
        setTimetable(res);
      } else {
        const res = await timetableApi.getDraftTimetable(selectedClassId);
        setTimetable(res);
      }
    } catch (err: unknown) {
      const respErr = err as { response?: { status?: number; data?: { detail?: { message?: string } } } };
      if (viewMode === "published" && respErr.response?.status === 404) {
        setTimetable(null);
        setTimetableError("No published timetable exists for this class yet. Switch to Draft to edit and publish.");
      } else {
        setTimetableError(
          respErr.response?.data?.detail?.message || "Failed to load timetable for this class."
        );
      }
    } finally {
      setIsLoadingTimetable(false);
    }
  }, [selectedClassId, viewMode]);

  useEffect(() => {
    loadTimetable();
  }, [loadTimetable]);

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
      const respErr = err as { response?: { data?: { detail?: { message?: string } } } };
      toast.error(respErr.response?.data?.detail?.message || "Failed to delete lecture entry.");
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
      if (checkRes.has_conflicts && checkRes.conflicts.length > 0) {
        setDetectedConflicts(checkRes.conflicts);
        setIsConflictModalOpen(true);
        setIsPublishing(false);
        return;
      }
      // No conflicts -> publish directly
      await timetableApi.publishTimetable(selectedClassId, false);
      toast.success("Timetable published successfully! Enrolled students can now view it.");
      setViewMode("published");
      await loadTimetable();
    } catch (err: unknown) {
      const respErr = err as { response?: { status?: number; data?: { detail?: { code?: string; message?: string; conflicts?: ConflictItem[] } } } };
      if (respErr.response?.status === 409 && respErr.response?.data?.detail?.conflicts) {
        setDetectedConflicts(respErr.response.data.detail.conflicts);
        setIsConflictModalOpen(true);
      } else {
        toast.error(
          respErr.response?.data?.detail?.message || "Failed to publish timetable."
        );
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
      setViewMode("published");
      await loadTimetable();
    } catch (err: unknown) {
      const respErr = err as { response?: { data?: { detail?: { message?: string } } } };
      toast.error(respErr.response?.data?.detail?.message || "Failed to publish timetable.");
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
      setViewMode("draft");
      await loadTimetable();
    } catch (err: unknown) {
      const respErr = err as { response?: { data?: { detail?: { message?: string } } } };
      toast.error(respErr.response?.data?.detail?.message || "Failed to unpublish timetable.");
    } finally {
      setIsUnpublishing(false);
    }
  };

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
                  ? "bg-primary text-text-inverse border-primary"
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
                  ? "bg-primary text-text-inverse border-primary"
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
      ) : (
        <WeeklyTimetableGrid
          entries={timetable?.entries || []}
          isLoading={isLoadingTimetable}
          canEdit={viewMode === "draft"}
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
