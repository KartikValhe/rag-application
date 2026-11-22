import React, { useId, useRef } from "react";
import { AlertTriangle } from "lucide-react";
import { Modal } from "./Modal";
import { Alert } from "./Alert";
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
  /** Keeps the confirm button off (for example when the item can no longer be changed). */
  confirmDisabled?: boolean;
  /** Plain-text error shown inside the dialog (announced as an alert). */
  error?: string | null;
  /** "cancel" puts the keyboard focus on Cancel when the dialog opens (use for destructive actions). */
  initialFocus?: "cancel";
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
  confirmDisabled = false,
  error,
  initialFocus,
  onConfirm,
  onClose,
}) => {
  const actualVariant = confirmVariant || variant || "danger";
  const cancelRef = useRef<HTMLButtonElement>(null);
  const titleId = useId();
  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      size="sm"
      labelledBy={titleId}
      closeOnBackdrop={!isLoading}
      initialFocusRef={initialFocus === "cancel" ? cancelRef : undefined}
      footer={
        <div className="flex flex-col-reverse sm:flex-row sm:justify-end gap-2.5 sm:gap-3 w-full">
          <Button
            ref={cancelRef}
            type="button"
            variant="outline"
            onClick={onClose}
            disabled={isLoading}
            className="w-full sm:w-auto min-h-[44px]"
          >
            {cancelText}
          </Button>
          <Button
            type="button"
            variant={actualVariant}
            isLoading={isLoading}
            disabled={confirmDisabled}
            onClick={onConfirm}
            className="w-full sm:w-auto min-h-[44px]"
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
        <div className="space-y-1 min-w-0 w-full">
          <h3 id={titleId} className="text-base font-semibold text-text break-words">{title}</h3>
          <p className="text-sm text-text-muted break-words">{message}</p>
        </div>
        {error && (
          <div className="w-full text-left break-words">
            <Alert variant="danger">{error}</Alert>
          </div>
        )}
      </div>
    </Modal>
  );
};
