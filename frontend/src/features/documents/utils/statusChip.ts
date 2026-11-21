export type StatusChipVariant = "default" | "primary" | "success" | "warning" | "danger" | "info";

export interface StatusChipResult {
  label: string;
  text: string;
  variant: StatusChipVariant;
}

export function getStatusChip(
  status: string | null | undefined,
  stage?: string | null,
  flaggedChunkCount?: number | null
): StatusChipResult {
  if (!status) {
    return { label: "Unknown", text: "Unknown", variant: "default" };
  }

  switch (status) {
    case "queued":
      return { label: "Waiting", text: "Waiting", variant: "info" };

    case "processing": {
      let stageText = "Processing";
      if (stage === "parsing") {
        stageText = "Reading file";
      } else if (stage === "chunking") {
        stageText = "Splitting text";
      } else if (stage === "scanning") {
        stageText = "Checking text";
      } else if (stage === "embedding" || stage === "indexing") {
        stageText = "Indexing";
      }
      return { label: stageText, text: stageText, variant: "info" };
    }

    case "ready": {
      if (flaggedChunkCount === 0) {
        return {
          label: "Draft, ready to publish",
          text: "Draft, ready to publish",
          variant: "primary",
        };
      }
      if (flaggedChunkCount !== null && flaggedChunkCount !== undefined && flaggedChunkCount > 0) {
        const noun = flaggedChunkCount === 1 ? "passage" : "passages";
        const label = `Draft, needs review (${flaggedChunkCount} ${noun})`;
        return { label, text: label, variant: "warning" };
      }
      return {
        label: "Draft, not checked yet",
        text: "Draft, not checked yet",
        variant: "warning",
      };
    }

    case "published":
      return { label: "Published", text: "Published", variant: "success" };

    case "failed":
      return { label: "Failed", text: "Failed", variant: "danger" };

    case "archived":
      return { label: "Older version", text: "Older version", variant: "default" };

    default:
      return { label: "Unknown", text: "Unknown", variant: "default" };
  }
}
