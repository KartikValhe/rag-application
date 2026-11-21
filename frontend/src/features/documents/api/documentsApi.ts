import type { AxiosInstance, AxiosRequestConfig } from "axios";
import {
  DocumentListSchema,
  DocumentSchema,
  DocumentStatusSchema,
  DocumentUploadLimitsSchema,
  DocumentVersionSchema,
  VersionFlagsSchema,
  validateResponse,
  type DocumentListItem,
  type DocumentItem,
  type DocumentStatusItem,
  type DocumentUploadLimitsItem,
  type DocumentVersionItem,
  type VersionFlagsItem,
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

export interface FlagsParams {
  afterOrdinal?: number | null;
  limit?: number;
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

export interface PublishOptions {
  /** Only the boolean true is ever sent; anything else leaves the body empty. */
  acknowledgeFlags?: boolean;
}

/**
 * The ONE place that builds the publish request body. The flag acknowledgement is sent only when the
 * caller passes exactly the boolean `true`; a string, a number, an object or a boxed Boolean can
 * never produce it (the server answers 422 to a non-boolean, and a truthy guess must not acknowledge).
 */
export function buildPublishBody(acknowledge: unknown): { acknowledge_flags?: true } {
  if (acknowledge === true) {
    return { acknowledge_flags: true };
  }
  return {};
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
    flags: async (
      documentId: string,
      versionId: string,
      options?: FlagsParams
    ): Promise<VersionFlagsItem> => {
      const params: Record<string, unknown> = {
        limit: options?.limit ?? 50,
      };
      if (options?.afterOrdinal != null) {
        params.after_ordinal = options.afterOrdinal;
      }
      const response = await client.get(
        `/documents/${documentId}/versions/${versionId}/flags`,
        { params }
      );
      return validateResponse(VersionFlagsSchema, response.data, "document flags");
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
    publishVersion: async (
      documentId: string,
      versionId: string,
      options?: PublishOptions
    ): Promise<DocumentVersionItem> => {
      const response = await client.post(
        `/documents/${documentId}/versions/${versionId}/publish`,
        buildPublishBody(options?.acknowledgeFlags)
      );
      return validateResponse(DocumentVersionSchema, response.data, "document publish");
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
