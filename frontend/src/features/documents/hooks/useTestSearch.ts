import { useCallback, useEffect, useRef, useState } from "react";
import { documentsApi as defaultDocumentsApi } from "../api/documentsClient.ts";
import type { DocumentsApi, TestSearchInput } from "../api/documentsApi.ts";
import type { TestSearchResponseItem } from "../schemas/documentSchemas.ts";

export type TestSearchState =
  | { status: "idle" }
  | { status: "pending" }
  | { status: "success"; data: TestSearchResponseItem; includeDrafts: boolean }
  | { status: "error"; error: unknown };

/**
 * Read-only search call. It keeps the last answer in memory only: no query cache, no storage and
 * nothing about the question after the call (only whether drafts were included, to word the empty state).
 * A second call while one is running is ignored, so a double click or a repeated submit sends one request.
 */
export function useTestSearch(options: { api?: DocumentsApi } = {}) {
  const api = options.api ?? defaultDocumentsApi;
  const [state, setState] = useState<TestSearchState>({ status: "idle" });
  const inFlight = useRef(false);
  const controller = useRef<AbortController | null>(null);
  const mounted = useRef(true);
  // Bumped by reset(); an answer that belongs to an older generation is dropped.
  const generation = useRef(0);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      controller.current?.abort();
    };
  }, []);

  const run = useCallback(
    async (input: TestSearchInput) => {
      if (inFlight.current) return;
      inFlight.current = true;
      const abort = new AbortController();
      controller.current = abort;
      const myGeneration = generation.current;
      setState({ status: "pending" });
      try {
        const data = await api.search(input, { signal: abort.signal });
        if (mounted.current && generation.current === myGeneration) {
          setState({ status: "success", data, includeDrafts: input.includeDrafts === true });
        }
      } catch (error) {
        if (mounted.current && generation.current === myGeneration) setState({ status: "error", error });
      } finally {
        if (generation.current === myGeneration) inFlight.current = false;
      }
    },
    [api]
  );

  /** Forget the last answer (and any request still running), e.g. when the search scope changed. */
  const reset = useCallback(() => {
    generation.current += 1;
    controller.current?.abort();
    inFlight.current = false;
    setState((prev) => (prev.status === "idle" ? prev : { status: "idle" }));
  }, []);

  return { state, run, reset, isPending: state.status === "pending" };
}
