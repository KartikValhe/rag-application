import React from "react";
import { AlertTriangle } from "lucide-react";
import { Modal } from "./Modal";
import { Button, ButtonVariant } from "./Button";

export interface ConfirmDialogProps {
  isOpen: boolean;
  title: string;
  message: string;
  confirmText?: string;
  cancelText?: string;
  confirmVariant?: ButtonVariant;
  variant?: ButtonVariant;
  isLoading?: boolean;
  onConfirm: () => void | Promise<void>;
  onClose: () => void;
}

export const ConfirmDialog: React.FC<ConfirmDialogProps> = ({
  isOpen,
  title,
  message,
  confirmText = "Confirm",
  cancelText = "Cancel",
  confirmVariant,
  variant,
  isLoading = false,
  onConfirm,
  onClose,
}) => {
  const actualVariant = confirmVariant || variant || "danger";
  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      size="sm"
      closeOnBackdrop={!isLoading}
          footer={
            <div className="flex flex-col-reverse sm:flex-row sm:justify-end gap-2.5 sm:gap-3 w-full">
              <Button
                type="button"
                variant="outline"
                onClick={onClose}
                disabled={isLoading}
                className="w-full sm:w-auto"
              >
                {cancelText}
              </Button>
              <Button
                type="button"
                variant={actualVariant}
                isLoading={isLoading}
                onClick={onConfirm}
                className="w-full sm:w-auto"
              >
                {confirmText}
              </Button>
            </div>
          }
    >
          <div className="flex flex-col items-center text-center p-2 space-y-3">
            <div className="flex h-12 w-12 items-center justify-center rounded-full bg-danger-surface text-danger shrink-0">
              <AlertTriangle className="h-6 w-6" />
            </div>
            <div className="space-y-1">
              <h3 className="text-base font-semibold text-text">{title}</h3>
              <p className="text-sm text-text-muted">{message}</p>
            </div>
          </div>
        </Modal>
        );
};
