import React, { useState, useRef, useId } from "react";
import { cn } from "@/utils/cn";
import { UploadCloud, File, X } from "lucide-react";
import { sanitizeDisplayFilename } from "@/features/documents/utils/fileChecks";

export interface FileUploadProps {
  id?: string;
  accept?: string;
  onFileSelect: (file: File | null) => void;
  selectedFile: File | null;
  helperText?: string;
  error?: string;
  className?: string;
  disabled?: boolean;
}

export const FileUpload: React.FC<FileUploadProps> = ({
  id: customId,
  accept,
  onFileSelect,
  selectedFile,
  helperText,
  error,
  className,
  disabled = false,
}) => {
  const generatedId = useId();
  const inputId = customId || generatedId;
  const helperId = `${inputId}-helper`;
  const errorId = `${inputId}-error`;

  const [isDragOver, setIsDragOver] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    if (disabled) return;
    setIsDragOver(true);
  };

  const handleDragLeave = (e: React.DragEvent) => {
    e.preventDefault();
    setIsDragOver(false);
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    if (disabled) return;
    setIsDragOver(false);
    if (e.dataTransfer.files && e.dataTransfer.files[0]) {
      onFileSelect(e.dataTransfer.files[0]);
    }
  };

  const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files[0]) {
      onFileSelect(e.target.files[0]);
    }
  };

  const handleRemove = () => {
    onFileSelect(null);
    if (inputRef.current) inputRef.current.value = "";
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLButtonElement>) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      inputRef.current?.click();
    }
  };

  const describedBy = [
    helperText ? helperId : null,
    error ? errorId : null,
  ].filter(Boolean).join(" ") || undefined;

  return (
    <div className={cn("w-full space-y-1.5", className)}>
      <input
        ref={inputRef}
        type="file"
        id={inputId}
        accept={accept}
        onChange={handleChange}
        className="hidden"
        disabled={disabled}
        aria-describedby={describedBy}
      />

      {!selectedFile ? (
        <button
          type="button"
          disabled={disabled}
          onDragOver={handleDragOver}
          onDragLeave={handleDragLeave}
          onDrop={handleDrop}
          onClick={() => inputRef.current?.click()}
          onKeyDown={handleKeyDown}
          aria-describedby={describedBy}
          className={cn(
            "w-full flex flex-col items-center justify-center p-6 border-2 border-dashed border-border rounded-lg bg-surface hover:bg-surface-hover cursor-pointer transition-colors text-center focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2",
            isDragOver && "border-primary bg-primary-light/20",
            error && "border-danger",
            disabled && "opacity-50 cursor-not-allowed"
          )}
        >
          <UploadCloud className="h-8 w-8 text-text-subtle mb-2" />
          <span className="text-sm font-medium text-text block">
            Click to upload or drag and drop
          </span>
          {helperText && (
            <span id={helperId} className="text-xs text-text-muted mt-1 block">
              {helperText}
            </span>
          )}
        </button>
      ) : (
        <div className="flex items-center justify-between p-3 border border-border rounded-lg bg-surface-muted">
          <div className="flex items-center gap-2 overflow-hidden">
            <File className="h-5 w-5 text-primary shrink-0" />
            <div className="truncate">
              <span className="text-sm font-medium text-text truncate block">
                {sanitizeDisplayFilename(selectedFile.name)}
              </span>
              <span className="text-xs text-text-muted block">
                {(selectedFile.size / 1024).toFixed(1)} KB
              </span>
            </div>
          </div>
          <button
            type="button"
            onClick={handleRemove}
            aria-label="Remove file"
            className="p-1 rounded text-text-subtle hover:text-danger hover:bg-danger-surface transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
      )}

      {error && (
        <p id={errorId} className="text-xs text-danger" role="alert">
          {error}
        </p>
      )}
    </div>
  );
};

