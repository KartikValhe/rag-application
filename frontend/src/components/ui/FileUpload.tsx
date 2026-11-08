import React, { useState, useRef } from "react";
import { cn } from "@/utils/cn";
import { UploadCloud, File, X } from "lucide-react";

export interface FileUploadProps {
  accept?: string;
  onFileSelect: (file: File | null) => void;
  selectedFile: File | null;
  helperText?: string;
  error?: string;
  className?: string;
}

export const FileUpload: React.FC<FileUploadProps> = ({
  accept,
  onFileSelect,
  selectedFile,
  helperText,
  error,
  className,
}) => {
  const [isDragOver, setIsDragOver] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    setIsDragOver(true);
  };

  const handleDragLeave = (e: React.DragEvent) => {
    e.preventDefault();
    setIsDragOver(false);
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
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

  return (
    <div className={cn("w-full space-y-1.5", className)}>
      <input
        ref={inputRef}
        type="file"
        accept={accept}
        onChange={handleChange}
        className="hidden"
        id="file-upload-input"
      />

      {!selectedFile ? (
        <div
          onDragOver={handleDragOver}
          onDragLeave={handleDragLeave}
          onDrop={handleDrop}
          onClick={() => inputRef.current?.click()}
          className={cn(
            "flex flex-col items-center justify-center p-6 border-2 border-dashed border-border rounded-lg bg-surface hover:bg-surface-hover cursor-pointer transition-colors text-center",
            isDragOver && "border-primary bg-primary-light/20",
            error && "border-danger"
          )}
        >
          <UploadCloud className="h-8 w-8 text-text-subtle mb-2" />
          <p className="text-sm font-medium text-text">
            Click to upload or drag and drop
          </p>
          <p className="text-xs text-text-muted mt-1">
            {helperText || "Supported formats: .csv"}
          </p>
        </div>
      ) : (
        <div className="flex items-center justify-between p-3 border border-border rounded-lg bg-surface-muted">
          <div className="flex items-center gap-2 overflow-hidden">
            <File className="h-5 w-5 text-primary shrink-0" />
            <div className="truncate">
              <p className="text-sm font-medium text-text truncate">
                {selectedFile.name}
              </p>
              <p className="text-xs text-text-muted">
                {(selectedFile.size / 1024).toFixed(1)} KB
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={handleRemove}
            className="p-1 rounded text-text-subtle hover:text-danger hover:bg-danger-surface transition-colors"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
      )}

      {error && <p className="text-xs text-danger">{error}</p>}
    </div>
  );
};

