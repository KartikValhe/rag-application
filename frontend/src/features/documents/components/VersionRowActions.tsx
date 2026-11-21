import React from "react";
import { Lock, RefreshCw, Trash2 } from "lucide-react";
import { Button } from "@/components/ui";
import type { DocumentPolicyAction } from "../utils/documentPolicy";

export interface VersionRowActionsProps {
  /** Actions for this row, exactly as returned by getDocumentActions (the policy decides everything). */
  actions: DocumentPolicyAction[];
  /** Text for a failed version that a retry cannot fix (from the policy module), or null. */
  hint: string | null;
  /** The retry request for this row is running. */
  isRetrying: boolean;
  /** Any mutation for this row is running: all of its actions are disabled. */
  isBusy: boolean;
  /** Stable id used to link the reason / hint text to the buttons. */
  describedById: string;
  onRetry: () => void;
  onDelete: () => void;
}

/**
 * Renders the policy actions of one version row. It only renders; it never decides.
 * Mobile first: buttons fill the card width and are at least 44 px high. The destructive action
 * sits in its own block, separated by a border, so it is not tapped by accident.
 */
export const VersionRowActions: React.FC<VersionRowActionsProps> = ({
  actions,
  hint,
  isRetrying,
  isBusy,
  describedById,
  onRetry,
  onDelete,
}) => {
  const retry = actions.find((a) => a.id === "retry" && a.visible);
  const del = actions.find((a) => a.id === "delete_version" && a.visible);
  const lockReason = [retry, del].find((a) => a && !a.enabled && a.reason)?.reason;
  const noteText = lockReason ?? hint;

  if (!retry && !del && !hint) return null;

  return (
    <div className="w-full flex flex-col gap-3 md:flex-row md:flex-wrap md:items-center md:justify-end md:gap-2 min-w-0">
      {noteText && (
        <p
          id={describedById}
          className="w-full md:w-auto md:max-w-xs text-xs text-text-muted flex items-start gap-1.5 break-words text-left"
        >
          {lockReason && <Lock className="w-3.5 h-3.5 shrink-0 mt-0.5" aria-hidden="true" />}
          <span className="min-w-0">{noteText}</span>
        </p>
      )}

      {retry && (
        <div className="w-full md:w-auto">
          <Button
            variant="primary"
            className="w-full md:w-auto min-h-[44px]"
            leftIcon={<RefreshCw className="w-4 h-4" aria-hidden="true" />}
            isLoading={isRetrying}
            disabled={!retry.enabled || isBusy}
            aria-describedby={noteText ? describedById : undefined}
            onClick={onRetry}
          >
            Try again
          </Button>
        </div>
      )}

      {del && (
        <div className="w-full md:w-auto mt-1 pt-3 border-t border-border md:mt-0 md:pt-0 md:border-t-0 md:ml-3">
          <Button
            variant="outline"
            className="w-full md:w-auto min-h-[44px] text-danger border-danger/40"
            leftIcon={<Trash2 className="w-4 h-4" aria-hidden="true" />}
            disabled={!del.enabled || isBusy}
            aria-describedby={noteText ? describedById : undefined}
            onClick={onDelete}
          >
            Delete version
          </Button>
        </div>
      )}
    </div>
  );
};
