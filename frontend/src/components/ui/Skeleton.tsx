import { cn } from "@/lib/utils";

/**
 * Data-view loading state (spec section 22). Control actions get a
 * distinct "pending confirmation" state instead — see
 * ConfirmDialog.tsx — never this generic shimmer, since a control
 * call legitimately takes longer (it passes through the full risk/
 * arming gate chain) and that's a feature, not latency to hide.
 */
export function Skeleton({ className }: { className?: string }) {
  return (
    <div
      className={cn("animate-pulse rounded bg-gray-200 dark:bg-gray-800", className)}
      aria-hidden="true"
    />
  );
}

export function SkeletonRows({ rows = 3 }: { rows?: number }) {
  return (
    <div className="space-y-2">
      {Array.from({ length: rows }).map((_, i) => (
        <Skeleton key={i} className="h-6 w-full" />
      ))}
    </div>
  );
}
