import React, { useState, useMemo, useRef, useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Modal,
  Input,
  Select,
  Button,
  Alert,
  FileUpload,
} from "@/components/ui";
import { useToast } from "@/components/ui/Toast";
import { academicApi, type MyClassItem } from "@/features/academic/api/academicApi";
import { useUploadDocument, useUploadVersion } from "../hooks/useDocumentMutations";
import { useDocumentLimits } from "../hooks/useDocuments";
import { validateDocumentFile, DEFAULT_MAX_FILE_BYTES } from "../utils/fileChecks";
import { getDocumentError, type DocumentErrorInfo } from "../utils/documentErrors";
import { documentsApi as defaultDocumentsApi } from "../api/documentsClient";
import type { DocumentsApi } from "../api/documentsApi";
import type { DocumentVersionItem } from "../schemas/documentSchemas";

export interface UploadDialogProps {
  isOpen: boolean;
  onClose: () => void;
  onSuccess?: (version: DocumentVersionItem) => void;
  api?: DocumentsApi;
  initialSubjectId?: string;
  initialClassId?: string;
  /**
   * "document" (default): a new material with title, subject, classes and file.
   * "version": a new version of an existing material; only the file is asked for and sent.
   */
  mode?: "document" | "version";
  /** Required in "version" mode. */
  documentId?: string;
  /** "version" mode, duplicate_version: scroll to and focus that version's row (the dialog closes first). */
  onShowVersion?: (versionId: string) => void;
}

export const UploadDialog: React.FC<UploadDialogProps> = ({
  isOpen,
  onClose,
  onSuccess,
  api: customApi,
  initialSubjectId = "",
  initialClassId = "",
  mode = "document",
  documentId,
  onShowVersion,
}) => {
  const isVersionMode = mode === "version";
  const api = customApi ?? defaultDocumentsApi;

  // Optional toast helper (fails gracefully if ToastProvider is not mounted in test)
  let toastContext: ReturnType<typeof useToast> | null = null;
  try {
    toastContext = useToast();
  } catch {
    // No ToastProvider
  }

  // Form state
  const [title, setTitle] = useState("");
  const [subjectId, setSubjectId] = useState(initialSubjectId);
  const [selectedClassIds, setSelectedClassIds] = useState<string[]>(
    initialClassId ? [initialClassId] : []
  );
  const [selectedFile, setSelectedFile] = useState<File | null>(null);

  // Validation & Error state
  const [clientFileError, setClientFileError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [serverError, setServerError] = useState<DocumentErrorInfo | null>(null);

  // Upload progress & abort controller
  const [uploadProgress, setUploadProgress] = useState<number | null>(null);
  const abortControllerRef = useRef<AbortController | null>(null);

  // Reset or initialize on open
  useEffect(() => {
    if (isOpen) {
      setTitle("");
      setSubjectId(initialSubjectId);
      setSelectedClassIds(initialClassId ? [initialClassId] : []);
      setSelectedFile(null);
      setClientFileError(null);
      setFieldErrors({});
      setServerError(null);
      setUploadProgress(null);
    }
  }, [isOpen, initialSubjectId, initialClassId]);

  // Fetch upload limits
  const { data: uploadLimits } = useDocumentLimits({
    api,
    enabled: isOpen,
  });

  const maxFileBytes = uploadLimits?.max_file_bytes ?? DEFAULT_MAX_FILE_BYTES;
  const maxMb = Math.round(maxFileBytes / (1024 * 1024));
  const acceptedExts = uploadLimits?.accepted_extensions?.length
    ? uploadLimits.accepted_extensions
    : undefined;
  const acceptAttr = acceptedExts
    ? acceptedExts.map((e) => (e.startsWith(".") ? e : `.${e}`)).join(",")
    : ".pdf,.docx,.pptx,.txt,.md";
  const helperText = `Accepted formats: PDF, Word (.docx), PowerPoint (.pptx), text (.txt), Markdown (.md). Max ${maxMb} MB.`;

  // Fetch user classes & subjects
  const { data: myClasses = [] } = useQuery<MyClassItem[]>({
    queryKey: ["myClasses"],
    queryFn: () => academicApi.getMyClasses(),
    enabled: isOpen && !isVersionMode,
  });

  // Available subjects from user's classes
  const subjectOptions = useMemo(() => {
    const map = new Map<string, string>();
    for (const cls of myClasses) {
      for (const sub of cls.subjects) {
        if (!map.has(sub.id)) {
          map.set(sub.id, sub.name);
        }
      }
    }
    return Array.from(map.entries())
      .map(([id, name]) => ({ value: id, label: name }))
      .sort((a, b) => a.label.localeCompare(b.label));
  }, [myClasses]);

  // Classes offering the chosen subject
  const availableClasses = useMemo(() => {
    if (!subjectId) return [];
    return myClasses.filter((cls) => cls.subjects.some((sub) => sub.id === subjectId));
  }, [myClasses, subjectId]);

  const focusFirstError = (errors: Record<string, string>, hasGlobalAlert = false) => {
    requestAnimationFrame(() => {
      if (errors.title) {
        document.getElementById("material-title")?.focus();
      } else if (errors.subject) {
        document.getElementById("material-subject")?.focus();
      } else if (errors.classes) {
        const clsEl = document.querySelector<HTMLElement>('input[type="checkbox"]:not([disabled])');
        clsEl?.focus();
      } else if (errors.file) {
        const fileBtn =
          document.querySelector<HTMLElement>('button[aria-describedby*="material-file-upload"]') ||
          document.getElementById("material-file-upload");
        fileBtn?.focus();
      } else if (hasGlobalAlert) {
        const alertEl = document.querySelector<HTMLElement>('[role="alert"]');
        alertEl?.focus();
      }
    });
  };

  // Mutations: a new document and a new version share the success and error handling.
  const handleUploaded = (version: DocumentVersionItem) => {
    setUploadProgress(null);
    onClose();

    // Toast notification
    if (toastContext) {
      toastContext.success("Uploaded. We are reading the file now.");
      if (version.warnings && version.warnings.includes("duplicate_content")) {
        toastContext.info(
          "This file matches another material in your organisation. You can still publish it.",
          "Duplicate content notice"
        );
      }
    }

    onSuccess?.(version);
  };

  const handleUploadError = (err: unknown) => {
    setUploadProgress(null);
    const mapped = getDocumentError(err, isVersionMode ? "new_version" : "upload");
    setServerError(mapped);

    if (mapped.field) {
      const errObj = { [mapped.field]: mapped.message };
      setFieldErrors((prev) => ({ ...prev, ...errObj }));
      focusFirstError(errObj);
    } else {
      focusFirstError({}, true);
    }
  };

  const documentMutation = useUploadDocument({
    api,
    onSuccess: handleUploaded,
    onError: handleUploadError,
  });
  const versionMutation = useUploadVersion({
    api,
    onSuccess: handleUploaded,
    onError: handleUploadError,
  });

  const isUploading = documentMutation.isPending || versionMutation.isPending;

  const handleSubjectChange = (e: React.ChangeEvent<HTMLSelectElement>) => {
    const newSubjectId = e.target.value;
    setSubjectId(newSubjectId);
    setFieldErrors((prev) => {
      const copy = { ...prev };
      delete copy.subject;
      delete copy.classes;
      return copy;
    });

    // Prune classes that don't offer the new subject
    const validClasses = myClasses.filter((cls) =>
      cls.subjects.some((sub) => sub.id === newSubjectId)
    );
    setSelectedClassIds((prev) =>
      prev.filter((cid) => validClasses.some((c) => c.id === cid))
    );
  };

  const handleToggleClass = (classId: string) => {
    setSelectedClassIds((prev) => {
      const next = prev.includes(classId)
        ? prev.filter((id) => id !== classId)
        : [...prev, classId];
      if (next.length > 0) {
        setFieldErrors((curr) => {
          const copy = { ...curr };
          delete copy.classes;
          return copy;
        });
      }
      return next;
    });
  };

  const handleFileSelect = (file: File | null) => {
    setSelectedFile(file);
    setFieldErrors((prev) => {
      const copy = { ...prev };
      delete copy.file;
      return copy;
    });

    if (!file) {
      setClientFileError(null);
      return;
    }

    const err = validateDocumentFile(file, maxFileBytes, acceptedExts);
    setClientFileError(err);
  };

  const handleCancel = () => {
    if (isUploading && abortControllerRef.current) {
      abortControllerRef.current.abort();
    }
    setUploadProgress(null);
    onClose();
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (isUploading) return;

    // Clear previous errors
    setServerError(null);
    const newFieldErrors: Record<string, string> = {};

    const trimmedTitle = title.trim();
    if (!isVersionMode) {
      if (!trimmedTitle || trimmedTitle.length > 200) {
        newFieldErrors.title = "The title must not be empty (and at most 200 characters).";
      }

      if (!subjectId) {
        newFieldErrors.subject = "Please select a subject.";
      }

      if (selectedClassIds.length === 0) {
        newFieldErrors.classes = "Please select at least one class.";
      } else if (selectedClassIds.length > 50) {
        newFieldErrors.classes = "A material can be linked to at most 50 classes.";
      }
    }

    if (!selectedFile) {
      newFieldErrors.file = "Please select a file to upload.";
    } else {
      const fileErr = validateDocumentFile(selectedFile, maxFileBytes, acceptedExts);
      if (fileErr) {
        newFieldErrors.file = fileErr;
      }
    }

    if (Object.keys(newFieldErrors).length > 0) {
      setFieldErrors(newFieldErrors);
      focusFirstError(newFieldErrors);
      return;
    }

    setFieldErrors({});
    abortControllerRef.current = new AbortController();

    const onProgress = (p: number) => setUploadProgress(p);
    const signal = abortControllerRef.current.signal;

    if (isVersionMode) {
      if (!documentId) return;
      versionMutation.mutate({ documentId, form: { file: selectedFile! }, onProgress, signal });
      return;
    }

    documentMutation.mutate({
      form: {
        file: selectedFile!,
        title: trimmedTitle,
        subject_id: subjectId,
        class_ids: selectedClassIds,
      },
      onProgress,
      signal,
    });
  };

  const fileError = clientFileError || fieldErrors.file;
  const isSubmitDisabled = isUploading;

  return (
    <Modal
      isOpen={isOpen}
      onClose={handleCancel}
      title={isVersionMode ? "Upload new version" : "Upload material"}
      description={
        isVersionMode
          ? "Upload a new file for this material. Students keep seeing the published version until you publish this one."
          : "Upload a new document for your classes."
      }
      size="lg"
      footer={
        <div className="flex flex-col-reverse sm:flex-row sm:items-center sm:justify-end gap-2.5 sm:gap-3 w-full">
          <Button
            type="button"
            variant="secondary"
            onClick={handleCancel}
            disabled={false}
            className="w-full sm:w-auto min-h-[44px]"
          >
            Cancel
          </Button>
          <Button
            type="button"
            variant="primary"
            disabled={isSubmitDisabled}
            isLoading={isUploading}
            onClick={handleSubmit}
            className="w-full sm:w-auto min-h-[44px]"
          >
            Upload
          </Button>
        </div>
      }
    >
      <form onSubmit={handleSubmit} className="space-y-4 text-left">
        {/* Global/Server Alert Error (non-field errors or duplicate_version) */}
        {serverError && (isVersionMode ? serverError.field !== "file" : !serverError.field) && (
          <Alert variant="danger" title="Upload failed">
            <p className="break-words">{serverError.message}</p>
            {isVersionMode &&
              serverError.code === "duplicate_version" &&
              serverError.versionId &&
              onShowVersion && (
                <Button
                  type="button"
                  variant="secondary"
                  size="sm"
                  className="mt-3 w-full sm:w-auto min-h-[44px]"
                  onClick={() => {
                    const id = serverError.versionId!;
                    onClose();
                    onShowVersion(id);
                  }}
                >
                  Show it
                </Button>
              )}
          </Alert>
        )}

        {!isVersionMode && (
        <>
        {/* Title Field with Character Counter */}
        <div>
          <div className="flex items-center justify-between mb-1">
            <label htmlFor="material-title" className="block text-sm font-medium text-text">
              Title <span className="text-danger">*</span>
            </label>
            <span className="text-xs text-text-muted">
              {title.trim().length} / 200
            </span>
          </div>
          <Input
            id="material-title"
            aria-label="Title"
            placeholder="e.g. Chapter 4 Lecture Notes"
            value={title}
            onChange={(e) => {
              setTitle(e.target.value);
              if (fieldErrors.title) {
                setFieldErrors((prev) => {
                  const copy = { ...prev };
                  delete copy.title;
                  return copy;
                });
              }
            }}
            maxLength={200}
            error={fieldErrors.title}
            disabled={isUploading}
            required
          />
        </div>

        {/* Subject Select */}
        <div>
          <Select
            id="material-subject"
            label="Subject"
            aria-label="Subject"
            placeholder="Choose a subject..."
            options={subjectOptions}
            value={subjectId}
            onChange={handleSubjectChange}
            error={fieldErrors.subject}
            disabled={isUploading}
            required
          />
        </div>

        {/* Classes Checkbox List */}
        <div>
          <div className="flex items-center justify-between mb-1">
            <label className="block text-sm font-medium text-text">
              Classes <span className="text-danger">*</span>
            </label>
            <span className="text-xs text-text-muted">
              {selectedClassIds.length} selected (1-50)
            </span>
          </div>

          {!subjectId ? (
            <p className="text-xs text-text-muted italic border border-border border-dashed rounded-md p-3">
              Select a subject first to see available classes.
            </p>
          ) : availableClasses.length === 0 ? (
            <p className="text-xs text-text-muted italic border border-border border-dashed rounded-md p-3">
              No assigned classes offer this subject.
            </p>
          ) : (
            <div className="space-y-2 max-h-40 overflow-y-auto border border-border rounded-md p-3 bg-surface-muted/30">
              {availableClasses.map((cls) => {
                const checked = selectedClassIds.includes(cls.id);
                return (
                  <label
                    key={cls.id}
                    className="flex items-center gap-2.5 cursor-pointer text-sm select-none"
                  >
                    <input
                      type="checkbox"
                      aria-label={cls.name}
                      checked={checked}
                      onChange={() => handleToggleClass(cls.id)}
                      disabled={isUploading}
                      className="rounded border-border text-primary focus:ring-primary h-4 w-4"
                    />
                    <span className="text-text font-medium">{cls.name}</span>
                  </label>
                );
              })}
            </div>
          )}

          {fieldErrors.classes && (
            <p className="text-xs text-danger font-medium mt-1" role="alert">
              {fieldErrors.classes}
            </p>
          )}
        </div>

        </>
        )}

        {/* File Upload Zone */}
        <div>
          <label htmlFor="material-file-upload" className="block text-sm font-medium text-text mb-1">
            File <span className="text-danger">*</span>
          </label>
          <FileUpload
            id="material-file-upload"
            accept={acceptAttr}
            selectedFile={selectedFile}
            onFileSelect={handleFileSelect}
            error={fileError}
            helperText={helperText}
            disabled={isUploading}
          />
        </div>

        {/* Native Progress Bar with Text Label */}
        {isUploading && (
          <div className="space-y-1.5 pt-1">
            <div className="flex justify-between text-xs text-text-muted">
              <span id="upload-progress-label">Uploading...</span>
              <span>{uploadProgress !== null ? `${uploadProgress}%` : "Sending..."}</span>
            </div>
            <progress
              id="upload-progress-bar"
              aria-labelledby="upload-progress-label"
              value={uploadProgress ?? 0}
              max={100}
              className="w-full h-2 rounded overflow-hidden"
            />
          </div>
        )}
      </form>
    </Modal>
  );
};
