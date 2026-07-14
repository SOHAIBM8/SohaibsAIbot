import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { Card, CardHeader, CardTitle } from "@/components/ui/Card";
import { SkeletonRows } from "@/components/ui/Skeleton";
import { UnavailableNotice } from "@/components/ui/UnavailableNotice";
import { Badge } from "@/components/ui/Badge";
import type { DashboardOverviewOut } from "@/types/api";

export function DashboardPage() {
  const { data, isLoading } = useQuery({
    queryKey: ["dashboard", "overview"],
    queryFn: () => api.get<DashboardOverviewOut>("/api/dashboard/overview"),
  });

  if (isLoading || !data) {
    return <SkeletonRows rows={6} />;
  }

  return (
    <div className="grid gap-4 md:grid-cols-2">
      <Card>
        <CardHeader>
          <CardTitle>Equity curve</CardTitle>
        </CardHeader>
        {data.equity_curve.available ? (
          <ul className="max-h-64 space-y-1 overflow-y-auto text-sm">
            {data.equity_curve.snapshots.map((s) => (
              <li key={s.id} className="flex justify-between">
                <span className="text-gray-500">{new Date(s.snapshot_at).toLocaleString()}</span>
                <span className="font-mono">{s.equity.toFixed(2)}</span>
              </li>
            ))}
          </ul>
        ) : (
          <UnavailableNotice reason={data.equity_curve.reason ?? ""} />
        )}
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Open positions</CardTitle>
        </CardHeader>
        <UnavailableNotice reason={data.open_position_count.reason} />
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Today's PnL</CardTitle>
        </CardHeader>
        <UnavailableNotice reason={data.today_pnl.reason} />
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Latest AI daily summary</CardTitle>
        </CardHeader>
        {data.latest_daily_summary ? (
          <div>
            <div className="mb-2">
              <Badge tone="info">AI-generated narration</Badge>
            </div>
            <p className="text-sm text-gray-700 dark:text-gray-300">
              {data.latest_daily_summary.generated_text}
            </p>
            <p className="mt-2 text-xs text-gray-400">
              Generated {new Date(data.latest_daily_summary.generated_at).toLocaleString()}
            </p>
          </div>
        ) : (
          <p className="text-sm text-gray-500">No daily summary has been generated yet.</p>
        )}
      </Card>

      <Card className="md:col-span-2">
        <CardHeader>
          <CardTitle>Recent risk decisions</CardTitle>
        </CardHeader>
        {data.recent_risk_decisions.length === 0 ? (
          <p className="text-sm text-gray-500">No risk decisions recorded yet.</p>
        ) : (
          <ul className="space-y-2 text-sm">
            {data.recent_risk_decisions.map((d) => (
              <li key={d.id} className="flex items-center justify-between border-b border-gray-100 pb-2 dark:border-gray-800">
                <span>
                  {d.strategy_id} — approved {d.approved_quantity} / proposed {d.proposed_quantity}
                </span>
                {d.rejection_reason && <Badge tone="critical">{d.rejection_reason}</Badge>}
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
