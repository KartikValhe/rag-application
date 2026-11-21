import { useState, useEffect, useRef, useCallback } from "react";
import { useQuery } from "@tanstack/react-query";
import { documentsApi as defaultDocumentsApi } from "../api/documentsClient";
import type { DocumentsApi } from "../api/documentsApi";
import type { DocumentStatusItem } from "../schemas/documentSchemas";
import {
  nextPollDelay,
  isTerminalStatus,
  HARD_STOP_SECONDS,
  getLongRunningStatus,
  type LongRunningStatusInfo,
} from "../utils/pollingSchedule";

export interface UseVersionStatusOptions {
  documentId: string;
  versionId?: string | null;
  initialStatus?: string | null;
  initialStage?: string | null;
  createdAt?: string | null;
  api?: DocumentsApi;
  onTerminalStatus?: (status: DocumentStatusItem) => void;
  enabled?: boolean;
}

export function useVersionStatus({
  documentId,
  versionId,
  initialStatus,
  initialStage,
  createdAt,
  api: customApi,
  onTerminalStatus,
  enabled = true,
}: UseVersionStatusOptions) {
  const api = customApi ?? defaultDocumentsApi;
  const [attempt, setAttempt] = useState(0);
  const [consecutiveErrors, setConsecutiveErrors] = useState(0);
  const [isConnectionLost, setIsConnectionLost] = useState(false);
  const [lastGoodStatus, setLastGoodStatus] = useState<DocumentStatusItem | null>(null);
  const [elapsedSeconds, setElapsedSeconds] = useState(0);
  const terminalRef = useRef(false);

  // Track elapsed time since createdAt
  useEffect(() => {
    if (!createdAt) return;
    const startTime = new Date(createdAt).getTime();
    if (isNaN(startTime)) return;

    const updateElapsed = () => {
      const now = Date.now();
      const secs = Math.max(0, Math.floor((now - startTime) / 1000));
      setElapsedSeconds(secs);
    };

    updateElapsed();
    const timer = setInterval(updateElapsed, 1000);
    return () => clearInterval(timer);
  }, [createdAt]);

  const currentStatus = lastGoodStatus?.status ?? initialStatus;
  const isTerminal = isTerminalStatus(currentStatus);
  const isHardStop = elapsedSeconds >= HARD_STOP_SECONDS;

  // Polling is active only while version is in flight and not hard stop
  const shouldPoll =
    enabled &&
    Boolean(documentId && versionId) &&
    !isTerminal &&
    !isHardStop;

  const statusQuery = useQuery({
    queryKey: ["documentVersionStatus", documentId, versionId],
    queryFn: async () => {
      if (!versionId) throw new Error("No version ID");
      try {
        const result = await api.status(documentId, versionId);
        setIsConnectionLost(false);
        setConsecutiveErrors(0);
        setAttempt((prev) => prev + 1);
        setLastGoodStatus(result);

        if (isTerminalStatus(result.status) && !terminalRef.current) {
          terminalRef.current = true;
          onTerminalStatus?.(result);
        }

        return result;
      } catch (err: unknown) {
        const status = (err as { response?: { status?: number } })?.response?.status;
        if (status === 403 || status === 404) {
          throw err;
        }

        setIsConnectionLost(true);
        setConsecutiveErrors((prev) => prev + 1);
        throw err;
      }
    },
    enabled: shouldPoll,
    retry: false,
    refetchInterval: (query) => {
      if (!shouldPoll) return false;
      const status = query.state.error
        ? (query.state.error as { response?: { status?: number } })?.response?.status
        : null;
      if (status === 403 || status === 404) return false;
      return nextPollDelay(attempt, consecutiveErrors > 0 ? consecutiveErrors : false);
    },
    refetchIntervalInBackground: false,
  });

  const activeStatus = lastGoodStatus?.status ?? initialStatus;
  const activeStage = lastGoodStatus?.stage ?? initialStage;

  const longRunningInfo: LongRunningStatusInfo = getLongRunningStatus(
    activeStatus,
    activeStage,
    elapsedSeconds
  );

  const checkAgain = useCallback(() => {
    setElapsedSeconds(0);
    setAttempt(0);
    setConsecutiveErrors(0);
    setIsConnectionLost(false);
    terminalRef.current = false;
    statusQuery.refetch();
  }, [statusQuery]);

  return {
    statusData: lastGoodStatus ?? statusQuery.data ?? null,
    status: activeStatus,
    stage: activeStage,
    isTerminal,
    isConnectionLost,
    isHardStop: longRunningInfo.isHardStop,
    longRunningInfo,
    checkAgain,
    refetch: statusQuery.refetch,
    isLoading: statusQuery.isLoading && !lastGoodStatus,
  };
}
