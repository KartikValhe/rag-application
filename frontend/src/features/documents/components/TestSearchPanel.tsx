import React, { useEffect, useId, useMemo, useRef, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { AlertTriangle, CheckCircle2, Search } from "lucide-react";
import { Alert, Badge, Button, Card, Checkbox, Select } from "@/components/ui";
import { usePermission } from "@/hooks/usePermission";
import { getMaterialsBasePath } from "@/utils/roleUtils";
import type { DocumentsApi } from "../api/documentsApi";
import { useTestSearch } from "../hooks/useTestSearch";
import { useDelayedFlag } from "../hooks/useDelayedFlag";
import { getDocumentActions } from "../utils/documentPolicy";
import { getSearchInputMessage } from "../utils/documentErrors";
import {
  ASSISTANT_WOULD_NOT_USE_NOTE,
  DEFAULT_SEARCH_K,
  SEARCH_K_OPTIONS,
  SEARCH_MAX_QUERY_CHARS,
  buildSearchView,
  collapsePassage,
  describeSearchError,
  type SearchHitView,
} from "../utils/searchView";

export interface ScopeOption {
  id: string;
  name: string;
}

export interface TestSearchPanelProps {
  api?: DocumentsApi;
  classOptions: ScopeOption[];
  subjectOptions: ScopeOption[];
  initialClassId?: string;
  initialSubjectId?: string;
  /** Shown when the panel is mounted from one document: the search still covers the whole class and subject. */
  singleDocumentScope?: boolean;
}

export const SINGLE_DOCUMENT_SCOPE_NOTE =
  "This searches all published material in the chosen class and subject, not only this document.";

const SLOW_SEARCH_HINT_MS = 3000;

/**
 * Everything that comes back from a search is untrusted text (it may contain prompt-injection text).
 * It is rendered only as React text children, wrapped, never as HTML or Markdown, and never turned into a link.
 */
const PassageText: React.FC<{ text: string }> = ({ text }) => {
  const [expanded, setExpanded] = useState(false);
  const { preview, isTruncated } = useMemo(() => collapsePassage(text), [text]);
  const bodyId = useId();
  return (
    <div className="space-y-1 min-w-0">
      <p id={bodyId} className="text-sm text-text whitespace-pre-wrap break-words">
        {expanded || !isTruncated ? text : preview}
      </p>
      {isTruncated && (
        <Button
          type="button"
          size="sm"
          variant="ghost"
          className="min-h-[44px] px-2 text-xs"
          aria-expanded={expanded}
          aria-controls={bodyId}
          onClick={() => setExpanded((prev) => !prev)}
        >
          {expanded ? "Show less" : "Show more"}
        </Button>
      )}
    </div>
  );
};

const HitCard: React.FC<{ hit: SearchHitView; baseRoute: string }> = ({ hit, baseRoute }) => (
  <li className="min-w-0">
    <Card className="p-4 space-y-2.5 min-w-0 bg-surface border-border">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-semibold text-text-muted">{`#${hit.rank}`}</span>
        <Badge variant="secondary" size="sm">{hit.versionLabel}</Badge>
        {hit.statusLabel && (
          <Badge variant={hit.isDraft ? "warning" : "success"} size="sm">
            {hit.statusLabel}
          </Badge>
        )}
      </div>

      <h4 className="font-medium text-text break-words">
        <Link
          to={`${baseRoute}/${encodeURIComponent(hit.documentId)}`}
          className="break-words hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary rounded"
        >
          {hit.title}
        </Link>
      </h4>

      {hit.pageText && <p className="text-xs text-text-muted break-words">{hit.pageText}</p>}
      {hit.heading && <p className="text-xs font-semibold text-text break-words">{hit.heading}</p>}

      <p className="text-sm text-text break-words">
        {`Similarity ${hit.similarityText}${hit.thresholdText ? ` (${hit.thresholdText})` : ""}`}
      </p>
      {hit.keywordText && <p className="text-xs text-text-muted break-words">{hit.keywordText}</p>}

      <PassageText text={hit.content} />

      {hit.flagLabels.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {hit.flagLabels.map((label) => (
            <Badge key={label} variant="warning" size="sm" className="whitespace-normal break-words text-xs">
              {label}
            </Badge>
          ))}
        </div>
      )}
      {hit.titleFlagged && (
        <p className="text-xs text-warning break-words">The title of this material was flagged for review.</p>
      )}
      {hit.filenameFlagged && (
        <p className="text-xs text-warning break-words">The file name of this material was flagged for review.</p>
      )}

      <details className="text-xs text-text-muted">
        <summary className="flex min-h-[44px] cursor-pointer items-center rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary">
          Technical details
        </summary>
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 pb-2">
          {hit.technical.map((row) => (
            <React.Fragment key={row.label}>
              <dt className="font-medium">{row.label}</dt>
              <dd className="break-words">{row.value}</dd>
            </React.Fragment>
          ))}
        </dl>
      </details>
    </Card>
  </li>
);

export const TestSearchPanel: React.FC<TestSearchPanelProps> = ({
  api,
  classOptions,
  subjectOptions,
  initialClassId = "",
  initialSubjectId = "",
  singleDocumentScope = false,
}) => {
  const { can } = usePermission();
  const location = useLocation();
  const baseRoute = getMaterialsBasePath(location.pathname);
  const ids = {
    query: useId(),
    queryHelp: useId(),
    queryError: useId(),
    cls: useId(),
    subject: useId(),
    count: useId(),
  };

  const [query, setQuery] = useState("");
  const [classId, setClassId] = useState(initialClassId);
  const [subjectId, setSubjectId] = useState(initialSubjectId);
  // Follow the page filters: when a page filter changes while the panel is open, the panel takes the new value.
  const [seenFilters, setSeenFilters] = useState({ cls: initialClassId, subject: initialSubjectId });
  if (seenFilters.cls !== initialClassId || seenFilters.subject !== initialSubjectId) {
    if (seenFilters.cls !== initialClassId) setClassId(initialClassId);
    if (seenFilters.subject !== initialSubjectId) setSubjectId(initialSubjectId);
    setSeenFilters({ cls: initialClassId, subject: initialSubjectId });
  }
  const [includeDrafts, setIncludeDrafts] = useState(false);
  const [count, setCount] = useState(String(DEFAULT_SEARCH_K));
  const [localFieldError, setLocalFieldError] = useState<string | null>(null);

  const { state, run, reset, isPending } = useTestSearch({ api });
  // Results belong to the class and subject they were asked for: when either changes, drop them.
  useEffect(() => {
    reset();
  }, [classId, subjectId, reset]);

  const showSlowHint = useDelayedFlag(isPending, SLOW_SEARCH_HINT_MS);

  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const resultsHeadingRef = useRef<HTMLHeadingElement>(null);
  const errorRef = useRef<HTMLDivElement>(null);

  const view = useMemo(
    () => (state.status === "success" ? buildSearchView(state.data, { includeDrafts: state.includeDrafts }) : null),
    [state]
  );
  const errorInfo = useMemo(
    () => (state.status === "error" ? describeSearchError(state.error) : null),
    [state]
  );

  // Move focus to the results heading after a search, or to the error (beside the box when it is about the question).
  useEffect(() => {
    if (state.status === "success") {
      resultsHeadingRef.current?.focus();
    } else if (state.status === "error") {
      if (errorInfo?.field === "query") textareaRef.current?.focus();
      else errorRef.current?.focus();
    }
  }, [state, errorInfo]);

  const policy = getDocumentActions({ can }).find((a) => a.id === "test_search");
  if (!policy?.visible) return null;

  const fieldError = localFieldError ?? (errorInfo?.field === "query" ? errorInfo.message : null);
  const trimmedLength = query.trim().length;
  const isOverLimit = trimmedLength > SEARCH_MAX_QUERY_CHARS;
  const generalError = errorInfo && errorInfo.field !== "query" ? errorInfo.message : null;

  const handleSubmit = (event: React.FormEvent) => {
    event.preventDefault();
    if (isPending || trimmedLength === 0) return;
    if (isOverLimit) {
      setLocalFieldError(getSearchInputMessage("query_too_long"));
      textareaRef.current?.focus();
      return;
    }
    setLocalFieldError(null);
    void run({
      query,
      classId,
      subjectId,
      includeDrafts,
      k: Number(count),
    });
  };

  const announcement = isPending ? "Searching" : view ? view.announcement : "";

  return (
    <section aria-labelledby={`${ids.query}-title`} className="space-y-4 min-w-0">
      <div className="sr-only" aria-live="polite" aria-atomic="true">
        {announcement}
      </div>

      <div className="space-y-1">
        <h3 id={`${ids.query}-title`} className="flex items-center gap-2 text-base font-semibold text-text">
          <Search className="h-5 w-5 text-primary shrink-0" aria-hidden="true" />
          Test search
        </h3>
        <p className="text-xs sm:text-sm text-text-muted break-words">
          Type a question to see which passages the search would find. Your question is not saved.
        </p>
        {singleDocumentScope && (
          <p className="text-xs sm:text-sm text-text-muted break-words">{SINGLE_DOCUMENT_SCOPE_NOTE}</p>
        )}
      </div>

      <form onSubmit={handleSubmit} noValidate className="flex flex-col gap-4">
        <div className="space-y-1.5">
          <label htmlFor={ids.query} className="block text-sm font-medium text-text">
            Question
          </label>
          <textarea
            id={ids.query}
            ref={textareaRef}
            value={query}
            rows={3}
            onChange={(e) => {
              setQuery(e.target.value);
              setLocalFieldError(null);
            }}
            aria-invalid={fieldError ? true : undefined}
            aria-describedby={[ids.queryHelp, fieldError ? ids.queryError : null].filter(Boolean).join(" ")}
            className="w-full min-h-[96px] resize-y rounded-md border border-border bg-surface px-3 py-2 text-sm text-text break-words placeholder:text-text-subtle focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
            placeholder="For example: What is the speed of light?"
          />
          <div className="flex flex-wrap items-start justify-between gap-x-3 gap-y-1">
            <p id={ids.queryHelp} className="text-xs text-text-muted">
              Up to {SEARCH_MAX_QUERY_CHARS} characters.
            </p>
            <p className={`text-xs ${isOverLimit ? "text-danger font-medium" : "text-text-muted"}`}>
              {`${trimmedLength} / ${SEARCH_MAX_QUERY_CHARS}`}
            </p>
          </div>
          {fieldError && (
            <p id={ids.queryError} className="text-xs text-danger font-medium break-words">
              {fieldError}
            </p>
          )}
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
          <Select
            id={ids.cls}
            label="Class"
            value={classId}
            onChange={(e) => setClassId(e.target.value)}
            options={[
              { value: "", label: "All classes" },
              ...classOptions.map((c) => ({ value: c.id, label: c.name })),
            ]}
          />
          <Select
            id={ids.subject}
            label="Subject"
            value={subjectId}
            onChange={(e) => setSubjectId(e.target.value)}
            options={[
              { value: "", label: "All subjects" },
              ...subjectOptions.map((s) => ({ value: s.id, label: s.name })),
            ]}
          />
          <Select
            id={ids.count}
            label="Number of results"
            value={count}
            onChange={(e) => setCount(e.target.value)}
            options={SEARCH_K_OPTIONS.map((n) => ({ value: String(n), label: String(n) }))}
          />
        </div>

        <div className="space-y-1">
          <Checkbox
            label="Include drafts"
            description="Also search versions that are ready but not published"
            checked={includeDrafts}
            onChange={(e) => setIncludeDrafts(e.target.checked)}
          />
          <p className="text-xs text-text-muted break-words">
            Only published versions are searched unless you tick Include drafts.
          </p>
        </div>

        <div>
          <Button
            type="submit"
            className="w-full sm:w-auto min-h-[44px]"
            disabled={isPending || trimmedLength === 0}
            isLoading={isPending}
          >
            {isPending ? "Searching…" : "Search"}
          </Button>
        </div>
      </form>

      {showSlowHint && (
        <p className="text-xs text-text-muted break-words">
          Loading the search model, this can take a few seconds.
        </p>
      )}

      {generalError && (
        <div ref={errorRef} tabIndex={-1} className="rounded-lg focus:outline-none focus-visible:ring-2 focus-visible:ring-primary">
          <Alert variant="danger" title="Search failed">
            <span className="break-words">{generalError}</span>
          </Alert>
        </div>
      )}

      {view && (
        <div className="space-y-4 min-w-0">
          <h3
            ref={resultsHeadingRef}
            tabIndex={-1}
            className="text-base font-semibold text-text rounded focus:outline-none focus-visible:ring-2 focus-visible:ring-primary"
          >
            Search results
          </h3>

          <div
            className={`flex items-start gap-2 rounded-lg border p-3 text-sm break-words ${
              view.banner.variant === "success"
                ? "bg-success-surface text-success-text border-success/20"
                : view.banner.variant === "warning"
                ? "bg-warning-surface text-warning-text border-warning/20"
                : "bg-info-surface text-info-text border-info/20"
            }`}
          >
            {view.banner.variant === "success" ? (
              <CheckCircle2 className="h-5 w-5 shrink-0" aria-hidden="true" />
            ) : (
              <AlertTriangle className="h-5 w-5 shrink-0" aria-hidden="true" />
            )}
            <p className="min-w-0 break-words">{view.banner.text}</p>
          </div>

          <div className="space-y-2">
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-text-muted">
              <span>{view.thresholdText}</span>
              <span>{view.candidateText}</span>
              {view.calibration.calibrated && (
                <Badge variant="success" size="sm">{view.calibration.text}</Badge>
              )}
            </div>
            {!view.calibration.calibrated && (
              <div className="flex items-start gap-2 rounded-lg border border-warning/20 bg-warning-surface p-3 text-xs text-warning-text">
                <AlertTriangle className="h-4 w-4 shrink-0 mt-0.5" aria-hidden="true" />
                <p className="min-w-0 break-words">{view.calibration.text}</p>
              </div>
            )}
          </div>

          {view.hits.length > 0 && (
            <>
              {view.assistantWouldNotUse && (
                <p className="text-xs text-text-muted break-words">{ASSISTANT_WOULD_NOT_USE_NOTE}</p>
              )}
              <ol aria-label="Search results" className="space-y-3 min-w-0">
                {view.hits.map((hit) => (
                  <HitCard key={hit.key} hit={hit} baseRoute={baseRoute} />
                ))}
              </ol>
            </>
          )}
        </div>
      )}
    </section>
  );
};
