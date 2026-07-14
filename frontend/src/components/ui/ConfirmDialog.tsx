import { useState, type ReactNode } from "react";
import { Button } from "@/components/ui/Button";

interface ConfirmDialogProps {
  open: boolean;
  title: string;
  description: ReactNode;
  confirmLabel?: string;
  danger?: boolean;
  onConfirm: () => Promise<void>;
  onClose: () => void;
}

/**
 * The confirmation-dialog primitive every control action uses (spec
 * section 5/24) — kill-switch engage/disengage, arm/disarm, cancel-
 * order all route through this same component, not each rolling
 * their own.
 *
 * No optimistic UI updates for any control action affecting trading
 * state (spec decision #4): the dialog stays in "pending" — a
 * distinct state from a generic spinner (spec section 22) — until
 * onConfirm's promise actually resolves or rejects. The caller only
 * ever updates its own state from the real API response, never from
 * an assumed outcome.
 */
export function ConfirmDialog({
  open,
  title,
  description,
  confirmLabel = "Confirm",
  danger = false,
  onConfirm,
  onClose,
}: ConfirmDialogProps) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!open) return null;

  async function handleConfirm() {
    setPending(true);
    setError(null);
    try {
      await onConfirm();
      setPending(false);
      onClose();
    } catch (err) {
      setPending(false);
      setError(err instanceof Error ? err.message : "Request failed");
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40"
      role="dialog"
      aria-modal="true"
      data-testid="confirm-dialog"
    >
      <div className="w-full max-w-sm rounded-lg bg-white p-5 shadow-lg dark:bg-gray-900">
        <h3 className="text-base font-semibold text-gray-900 dark:text-gray-100">{title}</h3>
        <div className="mt-2 text-sm text-gray-600 dark:text-gray-400">{description}</div>
        {error && (
          <div
            data-testid="confirm-dialog-error"
            className="mt-3 rounded border border-red-200 bg-red-50 p-2 text-xs text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-300"
          >
            {error}
          </div>
        )}
        <div className="mt-4 flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose} disabled={pending}>
            Cancel
          </Button>
          <Button
            variant={danger ? "danger" : "primary"}
            onClick={handleConfirm}
            disabled={pending}
            data-testid="confirm-dialog-confirm"
          >
            {pending ? "Waiting for confirmation…" : confirmLabel}
          </Button>
        </div>
      </div>
    </div>
  );
}
