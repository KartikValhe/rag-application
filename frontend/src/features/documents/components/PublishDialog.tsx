import React, { useId, useRef, useState } from "react";
import { Lock, ShieldAlert, AlertTriangle } from "lucide-react";
import { Alert, Button, Modal, Skeleton } from "@/components/ui";
import { useOptionalToast } from "@/components/ui/Toast";
import type { DocumentsApi } from "../api/documentsApi";
import type { DocumentItem, DocumentVersionItem } from "../schemas/documentSchemas";
import type { DocumentPolicyAction } from "../utils/documentPolicy";
import { useVersionFlags } from "../hooks/useVersionFlags";
import { usePublishVersion } from "../hooks/useDocumentMutations";
import { getDocumentError } from "../utils/documentErrors";
import { sanitizeDisplayFilename } from "../utils/fileChecks";
import { formatFlaggedCountHeading, TRIPWIRE_NOTICE } from "../utils/flagDescriptions";
import {
  needsFlagReview,
  formatReplacesLine,
  PUBLISH_TICK_TEXT,
  PUBLISH_REASON_TICK,
  PUBLISH_REASON_FLAGS_LOADING,
  PUBLISH_REASON_FLAGS_FAILED,
  PUBLISH_REASON_NOT_AVAILABLE,
} from "../utils/publishReview";
import { FlaggedItemCard, FlagSourceNotices } from "./FlagsViewer";

export interface PublishDialogProps {
  isOpen: boolean;
  onClose: () => void;
  document: DocumentItem;
  /** The version to publish, as the page currently knows it (it is reloaded after a conflict). */
  version: DocumentVersionItem;
  /** The "publish" action for this version, exactly as returned by getDocumentActions. */
  action: DocumentPolicyAction;
  api?: DocumentsApi;
  /** "Review all N": the page closes the dialog and opens the Flags tab for this version. */
  onReviewAll: (versionId: string) => void;
  onPublished?: (published: DocumentVersionItem, previouslyPublished: DocumentVersionItem | null) => void;
}

/**
 * Publish dialog. Two variants:
 * - simple confirm, only when the version is known to be clean (flagged_chunk_count === 0);
 * - flag review (flagged, never scanned, or the server asked for it): shows the flags and a REQUIRED,
 *   never pre-ticked checkbox. `acknowledge_flags: true` is sent only after it is ticked.
 * The body itself is built by buildPublishBody; this component only decides whether the user acknowledged.
 * The inner body is mounted only while open and keyed by version id, so every opening and every version
 * change starts with a fresh, unticked box.
 */
export const PublishDialog: React.FC<PublishDialogProps> = (props) => {
  if (!props.isOpen) return null;
  return <PublishDialogBody key={props.version.id} {...props} />;
};

const PublishDialogBody: React.FC<PublishDialogProps> = ({
  onClose,
  document: doc,
  version,
  action,
  api,
  onReviewAll,
  onPublished,
}) => {
  const toast = useOptionalToast();
  const reasonId = useId();
  const inFlightRef = useRef(false);
  const [acknowledged, setAcknowledged] = useState(false);
  // Set when the server answered flags_acknowledgement_required: holds its plain-text wording.
  const [serverNotice, setServerNotice] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const isReview = needsFlagReview(version.flagged_chunk_count, serverNotice !== null);
  const previous =
    doc.published_version && doc.published_version.id !== version.id ? doc.published_version : null;

  const flags = useVersionFlags({
    documentId: doc.id,
    versionId: version.id,
    api,
    enabled: isReview,
  });
  const flagsLoaded = flags.firstPage !== undefined;
  const flagsFailed = flags.isError && !flagsLoaded;
  const flaggedCount = flagsLoaded ? flags.flaggedChunkCount : version.flagged_chunk_count;

  const mutation = usePublishVersion({
    api,
    onSuccess: (published) => {
      inFlightRef.current = false;
      toast?.success(`Version ${published.version_no} is published.`);
      onPublished?.(published, previous);
      onClose();
    },
    onError: (err) => {
      inFlightRef.current = false;
      const info = getDocumentError(err, "publish");
      if (info.code === "flags_acknowledgement_required") {
        // Not an error: the server wants a review. Switch to the review variant, never re-send by itself.
        setServerNotice(info.message);
        setAcknowledged(false);
        setErrorMessage(null);
      } else {
        setErrorMessage(info.message);
      }
    },
  });

  const isPending = mutation.isPending;

  let disabledReason: string | null = null;
  if (!action.visible || !action.enabled) {
    disabledReason = action.reason ?? PUBLISH_REASON_NOT_AVAILABLE;
  } else if (isReview && !flagsLoaded) {
    disabledReason = flagsFailed ? PUBLISH_REASON_FLAGS_FAILED : PUBLISH_REASON_FLAGS_LOADING;
  } else if (isReview && !acknowledged) {
    disabledReason = PUBLISH_REASON_TICK;
  }

  const handleClose = () => {
    if (isPending) return;
    onClose();
  };

  const handlePublish = () => {
    if (inFlightRef.current || isPending || disabledReason !== null) return;
    inFlightRef.current = true;
    setErrorMessage(null);
    mutation.mutate({
      documentId: doc.id,
      versionId: version.id,
      // true only for a review dialog whose box is ticked; the builder turns anything else into {}
      acknowledgeFlags: isReview && acknowledged ? true : false,
    });
  };

  const reviewCount = typeof flaggedCount === "number" && flaggedCount > 0 ? flaggedCount : null;
  const fileName = sanitizeDisplayFilename(version.original_filename, 80);

  return (
    <Modal
      isOpen
      onClose={handleClose}
      size="md"
      title={`Publish version ${version.version_no}?`}
      closeOnBackdrop={!isPending}
      footer={
        <div className="flex flex-col gap-2.5 sm:flex-row-reverse sm:justify-start sm:gap-3 w-full">
          <Button
            type="button"
            variant="primary"
            className="w-full sm:w-auto min-h-[44px]"
            isLoading={isPending}
            disabled={disabledReason !== null}
            aria-describedby={disabledReason !== null ? reasonId : undefined}
            onClick={handlePublish}
          >
            Publish
          </Button>
          <Button
            type="button"
            variant="outline"
            className="w-full sm:w-auto min-h-[44px]"
            disabled={isPending}
            onClick={handleClose}
          >
            Cancel
          </Button>
        </div>
      }
    >
      <div className="space-y-4 min-w-0">
        <p className="text-sm text-text-muted break-words">{fileName}</p>

        {previous && (
          <p className="text-sm text-text break-words">{formatReplacesLine(previous.version_no)}</p>
        )}

        {serverNotice && (
          <Alert variant="warning">
            <span className="break-words">{serverNotice}</span>
          </Alert>
        )}

        {isReview && (
          <section className="space-y-3 min-w-0" aria-label="Flag review">
            {flagsFailed ? (
              <Alert variant="danger" title="Could not load the flagged passages">
                <div className="flex flex-col gap-3">
                  <span className="break-words">{getDocumentError(flags.error).message}</span>
                  <Button
                    type="button"
                    size="sm"
                    variant="secondary"
                    className="w-full min-h-[44px]"
                    onClick={() => flags.refetch()}
                  >
                    Try again
                  </Button>
                </div>
              </Alert>
            ) : !flagsLoaded ? (
              <div className="space-y-2" aria-hidden="true">
                <Skeleton className="h-5 w-2/3" />
                <Skeleton className="h-20 w-full" />
              </div>
            ) : null}

            {flagsLoaded && (
              <FlagSourceNotices
                titleFlagged={flags.titleFlagged}
                filenameFlagged={flags.filenameFlagged}
                originalFilename={fileName}
              />
            )}

            <div className="flex items-start gap-2">
              <ShieldAlert className="w-5 h-5 text-warning shrink-0 mt-0.5" aria-hidden="true" />
              <h3 className="text-sm font-semibold text-text break-words min-w-0">
                {formatFlaggedCountHeading(flaggedCount)}
              </h3>
            </div>

            <p className="text-xs text-text-muted flex items-start gap-1.5">
              <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5 text-warning" aria-hidden="true" />
              <span className="min-w-0">{TRIPWIRE_NOTICE}</span>
            </p>

            {flagsLoaded && flags.firstPage && flags.firstPage.items.length > 0 && (
              <div className="space-y-3">
                {flags.firstPage.items.map((item) => (
                  <FlaggedItemCard key={item.chunk_id} item={item} />
                ))}
              </div>
            )}

            {reviewCount !== null && (
              <Button
                type="button"
                variant="secondary"
                className="w-full sm:w-auto min-h-[44px]"
                onClick={() => onReviewAll(version.id)}
              >
                {`Review all ${reviewCount}`}
              </Button>
            )}

            <label className="flex items-start gap-3 min-h-[44px] py-2 cursor-pointer select-none">
              <input
                type="checkbox"
                required
                aria-required="true"
                checked={acknowledged}
                disabled={isPending}
                onChange={(event) => setAcknowledged(event.target.checked)}
                className="mt-0.5 h-5 w-5 shrink-0 cursor-pointer accent-primary"
              />
              <span className="text-sm text-text break-words min-w-0">{PUBLISH_TICK_TEXT}</span>
            </label>
          </section>
        )}

        {errorMessage && (
          <Alert variant="danger">
            <span className="break-words">{errorMessage}</span>
          </Alert>
        )}

        {disabledReason !== null && (
          <p id={reasonId} className="text-xs text-text-muted flex items-start gap-1.5 break-words">
            <Lock className="w-3.5 h-3.5 shrink-0 mt-0.5" aria-hidden="true" />
            <span className="min-w-0">{disabledReason}</span>
          </p>
        )}
      </div>
    </Modal>
  );
};
