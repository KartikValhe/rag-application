import React from "react";
import { Modal } from "@/components/ui/Modal";
import { Button } from "@/components/ui/Button";
import { AlertTriangle, AlertCircle, User, DoorOpen, School } from "lucide-react";
import { ConflictItem } from "../api/timetableApi";
import { formatConflictDetails } from "../utils/timetableHelpers";

export interface ConflictWarningDialogProps {
  isOpen: boolean;
  onClose: () => void;
  conflicts: ConflictItem[];
  onConfirmPublish: () => void;
  isPublishing?: boolean;
}

export const ConflictWarningDialog: React.FC<ConflictWarningDialogProps> = ({
  isOpen,
  onClose,
  conflicts,
  onConfirmPublish,
  isPublishing = false,
}) => {
  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title="Schedule Conflicts Detected"
      description="The timetable contains overlapping sessions with other classes or instructors in this organization."
      size="lg"
      footer={
        <div className="flex flex-col-reverse sm:flex-row items-center justify-end gap-3 w-full">
          <Button
            type="button"
            variant="secondary"
            onClick={onClose}
            disabled={isPublishing}
            className="w-full sm:w-auto min-h-[44px]"
          >
            Cancel & Return to Editor
          </Button>
          <Button
            type="button"
            variant="danger"
            onClick={onConfirmPublish}
            isLoading={isPublishing}
            className="w-full sm:w-auto min-h-[44px]"
          >
            Override & Publish Anyway
          </Button>
        </div>
      }
    >
      <div className="space-y-4">
        <div className="p-4 rounded-lg bg-warning/10 border border-warning/30 flex items-start gap-3">
          <AlertTriangle className="w-5 h-5 text-warning shrink-0 mt-0.5" />
          <div className="text-sm">
            <p className="font-semibold text-text">
              {conflicts.length} schedule {conflicts.length === 1 ? "conflict was" : "conflicts were"} identified across your tenant:
            </p>
            <p className="text-xs text-text-muted mt-1 leading-relaxed">
              Publishing will update the official schedule for enrolled students. You may proceed if these overlaps are intentional (e.g. shared labs or co-taught classes).
            </p>
          </div>
        </div>

        <div className="space-y-3 max-h-[50vh] overflow-y-auto pr-1">
          {conflicts.map((conflict, idx) => {
            const { title, description } = formatConflictDetails(conflict);
            const Icon =
              conflict.conflict_type === "teacher_overlap"
                ? User
                : conflict.conflict_type === "room_overlap"
                ? DoorOpen
                : conflict.conflict_type === "class_overlap"
                ? School
                : AlertCircle;

            return (
              <div
                key={idx}
                className="p-3.5 rounded-lg border border-border bg-surface flex items-start gap-3 transition-colors"
              >
                <div className="p-2 rounded-md bg-surface-hover text-text-muted shrink-0">
                  <Icon className="w-4 h-4" />
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-sm font-semibold text-text">{title}</span>
                  </div>
                  <p className="text-xs text-text-muted mt-1 leading-relaxed break-words">
                    {description}
                  </p>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </Modal>
  );
};

