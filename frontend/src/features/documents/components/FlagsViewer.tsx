import React, { useState } from "react";
import { Alert, Badge, Button, Card, Skeleton } from "@/components/ui";
import { Flag, AlertTriangle, ShieldAlert } from "lucide-react";
import type { DocumentsApi } from "../api/documentsApi";
import type { FlaggedChunkItem } from "../schemas/documentSchemas";
import { useVersionFlags } from "../hooks/useVersionFlags";
import { getDocumentError } from "../utils/documentErrors";
import {
  formatFlaggedCountHeading,
  formatPageRange,
  formatMatchSource,
  getFlagCodeDescription,
  TRIPWIRE_NOTICE,
  NO_LONGER_MATCHES_TEXT,
} from "../utils/flagDescriptions";

export interface FlagsViewerProps {
  documentId: string;
  versionId?: string | null;
  originalFilename?: string | null;
  api?: DocumentsApi;
}

const LONG_EXCERPT_THRESHOLD = 100;

export const ExcerptDisplay: React.FC<{ text: string }> = ({ text }) => {
  const [isExpanded, setIsExpanded] = useState(false);
  const isLong = text.length > LONG_EXCERPT_THRESHOLD;
  const displayText = isLong && !isExpanded ? `${text.slice(0, LONG_EXCERPT_THRESHOLD)}...` : text;

  return (
    <div className="space-y-1">
      <p className="text-xs sm:text-sm text-text whitespace-pre-wrap break-words">
        {displayText}
      </p>
      {isLong && (
        <Button
          type="button"
          size="sm"
          variant="ghost"
          className="text-xs h-7 px-2"
          onClick={() => setIsExpanded((prev) => !prev)}
        >
          {isExpanded ? "Show less" : "Show more"}
        </Button>
      )}
    </div>
  );
};

export const FlaggedItemCard: React.FC<{ item: FlaggedChunkItem }> = ({ item }) => {
  const matchedCodes = new Set((item.matches || []).map((m) => m.code));
  const hasMatches = Array.isArray(item.matches) && item.matches.length > 0;
  const unmatchedCodes = item.injection_flags.filter((code) => !matchedCodes.has(code));

  return (
    <Card className="p-4 space-y-3 bg-surface border-border">
      {/* Top row: Page/slide and Excluded marker */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-1.5 text-xs text-text-muted">
          <span className="font-medium text-text">Page/slide:</span>
          <span>{formatPageRange(item.page_start, item.page_end)}</span>
        </div>
        {item.excluded && (
          <Badge variant="secondary" size="sm">
            Excluded
          </Badge>
        )}
      </div>

      {/* Heading path */}
      {item.heading_path && (
        <p className="text-xs font-semibold text-text whitespace-pre-wrap break-words">
          {item.heading_path}
        </p>
      )}

      {/* Code chips wrapping */}
      <div className="flex flex-wrap gap-1.5">
        {item.injection_flags.map((code) => (
          <Badge
            key={code}
            variant="warning"
            size="sm"
            className="whitespace-normal break-words text-xs"
          >
            {getFlagCodeDescription(code)}
          </Badge>
        ))}
      </div>

      {/* Matched spans */}
      <div className="space-y-2 pt-1 border-t border-border-subtle">
        {hasMatches ? (
          <>
            {item.matches.map((match, idx) => (
              <div
                key={idx}
                className="bg-surface-muted/60 rounded-lg p-3 border border-border-subtle space-y-1.5"
              >
                <div className="flex items-center gap-1.5 text-xs text-text-muted">
                  <span className="font-medium text-text">
                    {formatMatchSource(match.source)}
                  </span>
                  {match.code && (
                    <span className="text-text-muted">
                      ({getFlagCodeDescription(match.code)})
                    </span>
                  )}
                </div>
                <ExcerptDisplay text={match.excerpt} />
              </div>
            ))}
            {/* Any stored code without a current match */}
            {unmatchedCodes.map((code) => (
              <p key={code} className="text-xs text-text-muted italic">
                {NO_LONGER_MATCHES_TEXT}
              </p>
            ))}
          </>
        ) : (
          /* Empty matches fallback to chunk excerpt */
          <div className="bg-surface-muted/60 rounded-lg p-3 border border-border-subtle space-y-1.5">
            <p className="text-xs text-text-muted italic">
              {NO_LONGER_MATCHES_TEXT}
            </p>
            <ExcerptDisplay text={item.excerpt} />
          </div>
        )}
      </div>
    </Card>
  );
};

/** Warnings for a flagged title or file name (shared by the Flags tab and the publish dialog). */
export const FlagSourceNotices: React.FC<{
  titleFlagged: boolean;
  filenameFlagged: boolean;
  originalFilename?: string | null;
}> = ({ titleFlagged, filenameFlagged, originalFilename }) => (
  <>
    {titleFlagged && (
      <Alert variant="warning" title="Title flagged">
        <p className="text-xs break-words">The title of this material was flagged for review.</p>
      </Alert>
    )}
    {filenameFlagged && (
      <Alert variant="warning" title="File name flagged">
        <p className="text-xs break-words">
          {originalFilename
            ? `The file name '${originalFilename}' was flagged for review.`
            : "The file name was flagged for review."}
        </p>
      </Alert>
    )}
  </>
);

export const FlagsViewer: React.FC<FlagsViewerProps> = ({
  documentId,
  versionId,
  originalFilename,
  api,
}) => {
  const {
    items,
    flaggedChunkCount,
    titleFlagged,
    filenameFlagged,
    isLoading,
    isError,
    error,
    refetch,
    hasNextPage,
    fetchNextPage,
    isFetchingNextPage,
    isFetchNextPageError,
  } = useVersionFlags({
    documentId,
    versionId,
    api,
  });

  // Initial loading skeleton
  if (isLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-6 w-1/3" />
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-32 w-full" />
      </div>
    );
  }

  // Initial error state (only when no items have been loaded yet)
  if (isError && items.length === 0) {
    const errorInfo = getDocumentError(error);
    return (
      <Alert variant="danger" title="Error loading flags">
        <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-3">
          <span className="break-words">{errorInfo.message}</span>
          <Button size="sm" variant="secondary" onClick={() => refetch()}>
            Try again
          </Button>
        </div>
      </Alert>
    );
  }

  return (
    <div className="space-y-4">
      {/* Title / file name flagged warning notes */}
      <FlagSourceNotices
        titleFlagged={titleFlagged}
        filenameFlagged={filenameFlagged}
        originalFilename={originalFilename}
      />


      {/* Heading section depending on flaggedChunkCount */}
      {flaggedChunkCount === null || flaggedChunkCount === undefined ? (
        <Alert variant="warning" title="Not checked yet">
          <p className="text-xs sm:text-sm break-words">
            {formatFlaggedCountHeading(null)}
          </p>
        </Alert>
      ) : flaggedChunkCount === 0 ? (
        <div className="p-4 bg-surface rounded-xl border border-border space-y-1">
          <h3 className="text-base font-semibold text-text">Nothing was flagged</h3>
          <p className="text-xs sm:text-sm text-text-muted">
            This material was checked and no passages were flagged for review.
          </p>
        </div>
      ) : (
        <div className="flex items-center gap-2">
          <ShieldAlert className="w-5 h-5 text-warning shrink-0" aria-hidden="true" />
          <h3 className="text-base font-semibold text-text">
            {formatFlaggedCountHeading(flaggedChunkCount)}
          </h3>
        </div>
      )}

      {/* Tripwire line */}
      <div className="p-3 bg-surface-muted/50 rounded-lg border border-border-subtle">
        <p className="text-xs text-text-muted flex items-start gap-1.5">
          <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5 text-warning" aria-hidden="true" />
          <span>{TRIPWIRE_NOTICE}</span>
        </p>
      </div>

      {/* Flag items stacked cards (never a wide table) */}
      {items.length > 0 && (
        <div className="space-y-3">
          {items.map((item) => (
            <FlaggedItemCard key={item.chunk_id} item={item} />
          ))}
        </div>
      )}

      {/* Next page error banner (keeps loaded items visible) */}
      {(isFetchNextPageError || (isError && items.length > 0)) && (
        <Alert variant="danger" title="Error loading more items">
          <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-3">
            <span className="break-words">
              {getDocumentError(error).message}
            </span>
            <Button size="sm" variant="secondary" onClick={() => fetchNextPage()}>
              Try again
            </Button>
          </div>
        </Alert>
      )}

      {/* Load more button */}
      {hasNextPage && (
        <div className="pt-2">
          <Button
            variant="secondary"
            className="w-full sm:w-auto min-h-[44px]"
            onClick={() => fetchNextPage()}
            isLoading={isFetchingNextPage}
            leftIcon={<Flag className="w-4 h-4" aria-hidden="true" />}
          >
            Load more
          </Button>
        </div>
      )}
    </div>
  );
};
