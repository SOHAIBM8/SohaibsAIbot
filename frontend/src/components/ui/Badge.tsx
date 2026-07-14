import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

export type BadgeTone = "neutral" | "success" | "warning" | "critical" | "info";

const TONE_CLASSES: Record<BadgeTone, string> = {
  neutral: "bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300",
  success: "bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-300",
  warning: "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300",
  critical: "bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300",
  info: "bg-blue-100 text-blue-800 dark:bg-blue-900/40 dark:text-blue-300",
};

/**
 * Severity is inherited directly from the backend's own event
 * definitions (spec section 18) — this component maps an already-
 * decided severity string to a color, it never invents one. The
 * backend's severity vocabulary (core/notifications/severity.py) is
 * exactly {critical, warning, info}; "success"/"neutral" are UI-only
 * additions for non-notification statuses (e.g. order state), not a
 * reinterpretation of that vocabulary.
 */
export function Badge({
  tone = "neutral",
  children,
  "data-testid": testId,
}: {
  tone?: BadgeTone;
  children: ReactNode;
  "data-testid"?: string;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium",
        TONE_CLASSES[tone],
      )}
      data-testid={testId}
    >
      {children}
    </span>
  );
}

export function severityToTone(severity: string): BadgeTone {
  if (severity === "critical") return "critical";
  if (severity === "warning") return "warning";
  if (severity === "info") return "info";
  return "neutral";
}
