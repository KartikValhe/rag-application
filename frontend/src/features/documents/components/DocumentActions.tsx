import React from "react";
import { Lock, Trash2, UploadCloud } from "lucide-react";
import { Button } from "@/components/ui";
import type { DocumentPolicyAction } from "../utils/documentPolicy";

export interface DocumentActionsProps {
  /** Document-level actions, exactly as returned by getDocumentActions (the policy decides everything). */
  actions: DocumentPolicyAction[];
  /** A mutation for this document is running: both actions are disabled. */
  isBusy: boolean;
  onNewVersion: () => void;
  onDeleteDocument: () => void;
}

/**
 * Renders the policy actions of one document ("New version", "Delete document"). It only renders; it never decides.
 * Mobile first: buttons fill the width and are at least 44 px high. The destructive action sits in its own
 * block under a border, so it is not tapped by accident. A disabled action always shows its reason as text.
 */
export const DocumentActions: React.FC<DocumentActionsProps> = ({
  actions,
  isBusy,
  onNewVersion,
  onDeleteDocument,
}) => {
  const newVersion = actions.find((a) => a.id === "new_version" && a.visible);
  const del = actions.find((a) => a.id === "delete_document" && a.visible);
  if (!newVersion && !del) return null;

  const reason = (id: string, action: DocumentPolicyAction) =>
    action.reason ? (
      <p
        id={id}
        className="mt-2 flex items-start gap-1.5 text-xs text-text-muted break-words text-left"
      >
        <Lock className="w-3.5 h-3.5 shrink-0 mt-0.5" aria-hidden="true" />
        <span className="min-w-0">{action.reason}</span>
      </p>
    ) : null;

  return (
    <div className="w-full flex flex-col gap-3 md:flex-row md:flex-wrap md:items-start md:gap-4 min-w-0">
      {newVersion && (
        <div className="w-full md:w-auto md:max-w-sm min-w-0">
          <Button
            variant="primary"
            className="w-full md:w-auto min-h-[44px]"
            leftIcon={<UploadCloud className="w-4 h-4" aria-hidden="true" />}
            disabled={!newVersion.enabled || isBusy}
            aria-describedby={newVersion.reason ? "new-version-reason" : undefined}
            onClick={onNewVersion}
          >
            New version
          </Button>
          {reason("new-version-reason", newVersion)}
        </div>
      )}

      {del && (
        <div className="w-full md:w-auto md:max-w-sm min-w-0 mt-1 pt-3 border-t border-border md:mt-0 md:pt-0 md:border-t-0 md:border-l md:pl-4">
          <Button
            variant="outline"
            className="w-full md:w-auto min-h-[44px] text-danger border-danger/40"
            leftIcon={<Trash2 className="w-4 h-4" aria-hidden="true" />}
            disabled={!del.enabled || isBusy}
            aria-describedby={del.reason ? "delete-document-reason" : undefined}
            onClick={onDeleteDocument}
          >
            Delete document
          </Button>
          {reason("delete-document-reason", del)}
        </div>
      )}
    </div>
  );
};
