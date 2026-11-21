import type { AxiosInstance } from "axios";
import {
  DocumentListSchema,
  DocumentSchema,
  DocumentUploadLimitsSchema,
  validateResponse,
  type DocumentListItem,
  type DocumentItem,
  type DocumentUploadLimitsItem,
} from "../schemas/documentSchemas.ts";

export interface DocumentListParams {
  class_id?: string;
  subject_id?: string;
  status?: string;
  cursor?: string;
  limit?: number;
}

export function createDocumentsApi(client: AxiosInstance) {
  return {
    list: async (params?: DocumentListParams): Promise<DocumentListItem> => {
      const response = await client.get("/documents", { params });
      return validateResponse(DocumentListSchema, response.data, "documents list");
    },
    get: async (documentId: string): Promise<DocumentItem> => {
      const response = await client.get(`/documents/${documentId}`);
      return validateResponse(DocumentSchema, response.data, "document detail");
    },
    limits: async (): Promise<DocumentUploadLimitsItem> => {
      const response = await client.get("/documents/limits");
      return validateResponse(DocumentUploadLimitsSchema, response.data, "upload limits");
    },
  };
}

export type DocumentsApi = ReturnType<typeof createDocumentsApi>;
