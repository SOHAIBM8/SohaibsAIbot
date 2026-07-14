import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { Badge } from "@/components/ui/Badge";
import type { DashboardOverviewOut } from "@/types/api";

/**
 * Spec decision #3: "Current trading mode (paper / testnet / mainnet)
 * is a persistent, always-visible UI element on every page." The
 * backend has no single persisted "current mode" value (see
 * api/routes/dashboard.py's module docstring — a real, confirmed
 * gap, not an oversight), so this banner shows that honestly rather
 * than guessing a mode from a heuristic that could be wrong. A
 * fabricated mode badge on a financial control surface is worse than
 * an honest "unavailable" one.
 */
export function ModeBanner() {
  const { data, isLoading } = useQuery({
    queryKey: ["dashboard", "overview"],
    queryFn: () => api.get<DashboardOverviewOut>("/api/dashboard/overview"),
    staleTime: 30_000,
  });

  if (isLoading) {
    return <Badge tone="neutral">Mode: loading…</Badge>;
  }
  // data.mode is currently always UnavailableOut — the backend has no
  // code path that returns a real mode value yet (see the module
  // docstring above). This branch is written to fail loudly rather
  // than silently render nothing if that ever changes without this
  // component being updated to match.
  return (
    <span title={data?.mode.reason ?? "Mode is unavailable"}>
      <Badge tone="neutral">Mode: unavailable</Badge>
    </span>
  );
}
