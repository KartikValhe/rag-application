import type { AxiosInstance, AxiosRequestConfig } from "axios";
import {
  DocumentListSchema,
  DocumentSchema,
  DocumentStatusSchema,
  DocumentUploadLimitsSchema,
  DocumentVersionSchema,
  validateResponse,
  type DocumentListItem,
  type DocumentItem,
  type DocumentStatusItem,
  type DocumentUploadLimitsItem,
  type DocumentVersionItem,
} from "../schemas/documentSchemas.ts";

export interface DocumentListParams {
  class_id?: string;
  subject_id?: string;
  status?: string;
  cursor?: string;
  limit?: number;
}

export interface DocumentUploadPayload {
  file: File;
  title: string;
  subject_id: string;
  class_ids: string[];
}

/** New version of an existing document: the file is the only thing the server accepts. */
export interface NewVersionPayload {
  file: File;
}

export interface UploadOptions {
  onProgress?: (progress: number) => void;
  signal?: AbortSignal;
}

export function buildUploadFormData(payload: DocumentUploadPayload): FormData {
  const form = new FormData();
  form.append("file", payload.file);
  form.append("title", payload.title);
  form.append("subject_id", payload.subject_id);
  for (const classId of payload.class_ids) {
    form.append("class_ids", classId);
  }
  return form;
}

export function buildNewVersionFormData(payload: NewVersionPayload): FormData {
  const form = new FormData();
  form.append("file", payload.file);
  return form;
}

// Shared by both upload routes: 10-minute timeout, progress and cancel.
function multipartConfig(options?: UploadOptions): AxiosRequestConfig {
  return {
    headers: { "Content-Type": "multipart/form-data" },
    timeout: 10 * 60 * 1000,
    signal: options?.signal,
    onUploadProgress: (progressEvent) => {
      if (options?.onProgress && progressEvent.total) {
        options.onProgress(Math.round((progressEvent.loaded * 100) / progressEvent.total));
      }
    },
  };
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
    status: async (documentId: string, versionId: string): Promise<DocumentStatusItem> => {
      const response = await client.get(`/documents/${documentId}/versions/${versionId}/status`);
      return validateResponse(DocumentStatusSchema, response.data, "document status");
    },
    upload: async (
      form: FormData | DocumentUploadPayload,
      options?: UploadOptions
    ): Promise<DocumentVersionItem> => {
      const data = form instanceof FormData ? form : buildUploadFormData(form);
      const response = await client.post("/documents", data, multipartConfig(options));
      return validateResponse(DocumentVersionSchema, response.data, "document upload");
    },
    uploadVersion: async (
      documentId: string,
      form: FormData | NewVersionPayload,
      options?: UploadOptions
    ): Promise<DocumentVersionItem> => {
      const data = form instanceof FormData ? form : buildNewVersionFormData(form);
      const response = await client.post(
        `/documents/${documentId}/versions`,
        data,
        multipartConfig(options)
      );
      return validateResponse(DocumentVersionSchema, response.data, "document new version");
    },
    retryVersion: async (documentId: string, versionId: string): Promise<DocumentVersionItem> => {
      const response = await client.post(`/documents/${documentId}/versions/${versionId}/retry`);
      return validateResponse(DocumentVersionSchema, response.data, "document retry");
    },
    deleteVersion: async (documentId: string, versionId: string): Promise<void> => {
      await client.delete(`/documents/${documentId}/versions/${versionId}`);
    },
    deleteDocument: async (documentId: string): Promise<void> => {
      await client.delete(`/documents/${documentId}`);
    },
  };
}

export type DocumentsApi = ReturnType<typeof createDocumentsApi>;
