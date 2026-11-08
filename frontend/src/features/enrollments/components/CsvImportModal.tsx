import React, { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Modal } from "@/components/ui/Modal";
import { Button } from "@/components/ui/Button";
import { Alert } from "@/components/ui/Alert";
import { enrollmentsApi, EnrollmentImportResponse } from "../api/enrollmentsApi";
import { ClassItem } from "@/features/academic/api/academicApi";
import { Upload, FileText, AlertCircle, CheckCircle, Table as TableIcon } from "lucide-react";

export interface CsvImportModalProps {
  isOpen: boolean;
  onClose: () => void;
  targetClass: ClassItem | null;
  onSuccess: (msg: string) => void;
}

export const CsvImportModal: React.FC<CsvImportModalProps> = ({
  isOpen,
  onClose,
  targetClass,
  onSuccess,
}) => {
  const queryClient = useQueryClient();
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [previewRows, setPreviewRows] = useState<string[][]>([]);
  const [importResult, setImportResult] = useState<EnrollmentImportResponse | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;

    if (!file.name.endsWith(".csv")) {
      setErrorMsg("Please upload a valid .csv file.");
      return;
    }

    setSelectedFile(file);
    setErrorMsg(null);
    setImportResult(null);

    // Read first few lines for preview
    const reader = new FileReader();
    reader.onload = (event) => {
      const text = event.target?.result as string;
      const lines = text
        .split(/\r?\n/)
        .map((l) => l.trim())
        .filter((l) => l.length > 0)
        .slice(0, 6) // Header + 5 rows
        .map((l) => l.split(",").map((cell) => cell.trim()));
      setPreviewRows(lines);
    };
    reader.readAsText(file);
  };

  const mutation = useMutation({
    mutationFn: () => {
      if (!targetClass || !selectedFile) throw new Error("Missing file or class");
      return enrollmentsApi.importEnrollmentsCsv(targetClass.id, selectedFile);
    },
    onSuccess: (res) => {
      setImportResult(res);
      queryClient.invalidateQueries({ queryKey: ["classRoster", targetClass?.id] });
      if (res.errors.length === 0) {
        onSuccess(`Successfully imported all ${res.succeeded} student enrollments.`);
      }
    },
    onError: (err: unknown) => {
      const axiosErr = err as { response?: { data?: { detail?: { message?: string } | string } } };
      const detail = axiosErr.response?.data?.detail;
      const message =
        typeof detail === "object" && detail?.message
          ? detail.message
          : typeof detail === "string"
          ? detail
          : "CSV Import failed. Please verify format.";
      setErrorMsg(message);
    },
  });

  const handleReset = () => {
    setSelectedFile(null);
    setPreviewRows([]);
    setImportResult(null);
    setErrorMsg(null);
  };

  if (!targetClass) return null;

  return (
    <Modal
      isOpen={isOpen}
      onClose={() => {
        handleReset();
        onClose();
      }}
      title={`Batch CSV Enrollment: ${targetClass.name}`}
      description="Upload a CSV with an 'email' column to bulk enroll students."
      size="lg"
    >
      <div className="space-y-5">
        {errorMsg && (
          <Alert variant="danger" title="Import Error">
            {errorMsg}
          </Alert>
        )}

        {/* File Select */}
        {!importResult && (
          <div className="space-y-4">
            <div className="border-2 border-dashed border-border rounded-xl p-6 text-center hover:bg-surface-hover/30 transition-colors">
              <Upload className="w-8 h-8 mx-auto text-primary mb-2" />
              <p className="text-sm font-semibold text-text">Choose CSV File to Upload</p>
              <p className="text-xs text-text-muted mt-1">
                CSV header should contain <code className="bg-surface px-1.5 py-0.5 rounded border border-border">email</code>
              </p>

              <label className="inline-block mt-4">
                <span className="btn btn-primary cursor-pointer text-xs">Browse CSV File</span>
                <input
                  type="file"
                  accept=".csv"
                  onChange={handleFileChange}
                  className="hidden"
                />
              </label>

              {selectedFile && (
                <div className="mt-3 flex items-center justify-center space-x-2 text-xs text-text font-medium">
                  <FileText className="w-4 h-4 text-primary" />
                  <span>{selectedFile.name} ({(selectedFile.size / 1024).toFixed(1)} KB)</span>
                </div>
              )}
            </div>

            {/* CSV Client Preview */}
            {previewRows.length > 0 && (
              <div>
                <div className="flex items-center space-x-2 text-xs font-semibold text-text-muted uppercase tracking-wider mb-2">
                  <TableIcon className="w-3.5 h-3.5" />
                  <span>File Preview (First {previewRows.length - 1} rows)</span>
                </div>
                <div className="overflow-x-auto rounded-lg border border-border bg-surface text-xs">
                  <table className="w-full text-left border-collapse">
                    <thead className="bg-surface-muted/60 border-b border-border text-text-muted font-semibold uppercase text-[11px]">
                      <tr>
                        {previewRows[0]?.map((col, idx) => (
                          <th key={`hdr-${idx}`} className="px-3 py-2">
                            {col}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-border text-text">
                      {previewRows.slice(1).map((row, rIdx) => (
                        <tr key={`r-${rIdx}`}>
                          {row.map((cell, cIdx) => (
                            <td key={`c-${cIdx}`} className="px-3 py-1.5">
                              {cell}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
          </div>
        )}

        {/* Results & Per-Row Errors Display */}
        {importResult && (
          <div className="space-y-4">
            <div className="grid grid-cols-3 gap-3 text-center">
              <div className="p-3 bg-surface-muted/40 rounded-lg border border-border">
                <span className="block text-xs text-text-muted font-semibold uppercase">Total Rows</span>
                <span className="text-xl font-bold text-text">{importResult.total_rows}</span>
              </div>
              <div className="p-3 bg-success/10 rounded-lg border border-success/30">
                <span className="block text-xs text-success font-semibold uppercase">Enrolled</span>
                <span className="text-xl font-bold text-success">{importResult.succeeded}</span>
              </div>
              <div className="p-3 bg-danger/10 rounded-lg border border-danger/30">
                <span className="block text-xs text-danger font-semibold uppercase">Errors</span>
                <span className="text-xl font-bold text-danger">{importResult.errors.length}</span>
              </div>
            </div>

            {importResult.errors.length === 0 ? (
              <div className="p-4 rounded-lg bg-success/10 border border-success/30 text-center text-sm text-success flex items-center justify-center space-x-2">
                <CheckCircle className="w-5 h-5 shrink-0" />
                <span>All records imported and enrolled without any errors.</span>
              </div>
            ) : (
              <div>
                <div className="flex items-center space-x-1.5 text-xs font-semibold text-danger uppercase tracking-wider mb-2">
                  <AlertCircle className="w-4 h-4" />
                  <span>Row Validation Errors ({importResult.errors.length})</span>
                </div>
                <div className="max-h-60 overflow-y-auto rounded-lg border border-border bg-surface text-xs">
                  <table className="w-full text-left border-collapse">
                    <thead className="bg-surface-muted/60 border-b border-border text-text-muted font-semibold uppercase text-[11px] sticky top-0">
                      <tr>
                        <th className="px-3 py-2 w-16">Row #</th>
                        <th className="px-3 py-2">Identifier</th>
                        <th className="px-3 py-2">Error Reason</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-border">
                      {importResult.errors.map((err, idx) => (
                        <tr key={`err-${idx}`} className="hover:bg-danger/5">
                          <td className="px-3 py-2 font-mono font-bold text-danger">
                            {err.row}
                          </td>
                          <td className="px-3 py-2 font-medium text-text">
                            {err.email || "—"}
                          </td>
                          <td className="px-3 py-2 text-danger">
                            {err.error}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
          </div>
        )}

        {/* Modal Footer */}
        <div className="flex justify-end gap-3 pt-3 border-t border-border">
          {importResult ? (
            <Button
              variant="outline"
              onClick={() => {
                handleReset();
                onClose();
              }}
            >
              Done
            </Button>
          ) : (
            <>
              <Button variant="outline" onClick={onClose}>
                Cancel
              </Button>
              <Button
                disabled={!selectedFile || mutation.isPending}
                isLoading={mutation.isPending}
                onClick={() => mutation.mutate()}
                leftIcon={<Upload className="w-4 h-4" />}
              >
                Upload & Process
              </Button>
            </>
          )}
        </div>
      </div>
    </Modal>
  );
};
