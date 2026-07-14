import { useQuery } from "@tanstack/react-query";
import { api, ApiError } from "@/lib/api";
import { Card, CardHeader, CardTitle } from "@/components/ui/Card";
import { SkeletonRows } from "@/components/ui/Skeleton";
import { UnavailableNotice } from "@/components/ui/UnavailableNotice";
import type { AccountOut, EquityCurveResponseOut } from "@/types/api";

/**
 * Exposure/loss-limit figures are deliberately NOT duplicated here —
 * spec section 10 asks for them, but Step 6's own research found no
 * live-PortfolioView data source exists; the real, persisted signal
 * (risk_decision_log) is already the Risk page's job. See the Risk
 * page for that data instead of a second, redundant fetch of it here.
 */
export function PortfolioPage() {
  const accountQuery = useQuery({
    queryKey: ["portfolio", "account"],
    queryFn: () => api.get<AccountOut>("/api/portfolio/account"),
    retry: false,
  });
  const equityQuery = useQuery({
    queryKey: ["portfolio", "equity-curve"],
    queryFn: () => api.get<EquityCurveResponseOut>("/api/portfolio/equity-curve"),
  });

  return (
    <div className="grid gap-4 md:grid-cols-2">
      <Card>
        <CardHeader>
          <CardTitle>Account</CardTitle>
        </CardHeader>
        {accountQuery.isLoading && <SkeletonRows rows={3} />}
        {accountQuery.isError &&
          (accountQuery.error instanceof ApiError && accountQuery.error.status === 404 ? (
            <p className="text-sm text-gray-500">No paper account exists yet for this account_id.</p>
          ) : (
            <p className="text-sm text-red-600">Failed to load account.</p>
          ))}
        {accountQuery.data && (
          <dl className="space-y-1 text-sm">
            <div className="flex justify-between">
              <dt className="text-gray-500">Starting balance</dt>
              <dd className="font-mono">{accountQuery.data.starting_balance.toFixed(2)}</dd>
            </div>
            <div className="flex justify-between">
              <dt className="text-gray-500">Current cash</dt>
              <dd className="font-mono">{accountQuery.data.current_cash.toFixed(2)}</dd>
            </div>
          </dl>
        )}
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Equity curve</CardTitle>
        </CardHeader>
        {equityQuery.isLoading && <SkeletonRows rows={3} />}
        {equityQuery.data &&
          (equityQuery.data.available ? (
            <ul className="max-h-64 space-y-1 overflow-y-auto text-sm">
              {equityQuery.data.snapshots.map((s) => (
                <li key={s.id} className="flex justify-between">
                  <span className="text-gray-500">{new Date(s.snapshot_at).toLocaleString()}</span>
                  <span className="font-mono">{s.equity.toFixed(2)}</span>
                </li>
              ))}
            </ul>
          ) : (
            <UnavailableNotice reason={equityQuery.data.reason ?? ""} />
          ))}
      </Card>
    </div>
  );
}
