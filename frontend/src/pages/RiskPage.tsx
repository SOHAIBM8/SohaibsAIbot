import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "@/lib/api";
import { Card, CardHeader, CardTitle } from "@/components/ui/Card";
import { SkeletonRows } from "@/components/ui/Skeleton";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import type {
  ArmingStateOut,
  CircuitBreakerStateOut,
  KillSwitchStateOut,
  RiskConfigOut,
  RiskDecisionRecordOut,
} from "@/types/api";

export function RiskPage() {
  const queryClient = useQueryClient();
  const [dialog, setDialog] = useState<"engage" | "disengage" | null>(null);
  const [reason, setReason] = useState("");

  const configQuery = useQuery({
    queryKey: ["risk", "config"],
    queryFn: () => api.get<RiskConfigOut>("/api/risk/config"),
  });
  const killSwitchQuery = useQuery({
    queryKey: ["risk", "kill-switch"],
    queryFn: () => api.get<KillSwitchStateOut>("/api/risk/kill-switch"),
  });
  const breakersQuery = useQuery({
    queryKey: ["risk", "circuit-breakers"],
    queryFn: () => api.get<CircuitBreakerStateOut[]>("/api/risk/circuit-breakers"),
  });
  const decisionsQuery = useQuery({
    queryKey: ["risk", "decisions"],
    queryFn: () => api.get<RiskDecisionRecordOut[]>("/api/risk/decisions", { limit: 20 }),
  });

  // No optimistic updates for any control action affecting trading
  // state (spec decision #4) — the kill-switch badge only ever
  // changes once this refetch, driven by the real API response,
  // resolves. ConfirmDialog's own "pending" state is what the user
  // sees in between.
  async function refetchKillSwitch() {
    await queryClient.invalidateQueries({ queryKey: ["risk", "kill-switch"] });
  }

  return (
    <div className="grid gap-4 md:grid-cols-2">
      <Card>
        <CardHeader>
          <CardTitle>Kill switch</CardTitle>
        </CardHeader>
        {killSwitchQuery.isLoading && <SkeletonRows rows={2} />}
        {killSwitchQuery.data && (
          <div className="text-sm">
            <Badge
              tone={killSwitchQuery.data.engaged ? "critical" : "success"}
              data-testid="kill-switch-status"
            >
              {killSwitchQuery.data.engaged ? "Engaged" : "Disengaged"}
            </Badge>
            {killSwitchQuery.data.engaged && (
              <p className="mt-2 text-gray-600 dark:text-gray-400">
                {killSwitchQuery.data.engaged_reason} — by {killSwitchQuery.data.engaged_by}
              </p>
            )}
            <div className="mt-3">
              {killSwitchQuery.data.engaged ? (
                <Button
                  variant="primary"
                  onClick={() => setDialog("disengage")}
                  data-testid="kill-switch-disengage-open"
                >
                  Disengage kill switch
                </Button>
              ) : (
                <Button
                  variant="danger"
                  onClick={() => {
                    setReason("");
                    setDialog("engage");
                  }}
                  data-testid="kill-switch-engage-open"
                >
                  Engage kill switch
                </Button>
              )}
            </div>
          </div>
        )}
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Circuit breakers</CardTitle>
        </CardHeader>
        {breakersQuery.isLoading && <SkeletonRows rows={2} />}
        {breakersQuery.data && breakersQuery.data.length === 0 && (
          <p className="text-sm text-gray-500">No circuit breaker events recorded yet.</p>
        )}
        {breakersQuery.data && breakersQuery.data.length > 0 && (
          <ul className="space-y-1 text-sm">
            {breakersQuery.data.map((b) => (
              <li key={b.breaker_name} className="flex items-center justify-between">
                <span>{b.breaker_name}</span>
                <Badge tone={b.tripped ? "critical" : "success"}>
                  {b.tripped ? "Tripped" : "Clear"}
                </Badge>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card className="md:col-span-2">
        <CardHeader>
          <CardTitle>Risk config</CardTitle>
        </CardHeader>
        {configQuery.isLoading && <SkeletonRows rows={3} />}
        {configQuery.data && (
          <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm md:grid-cols-4">
            {Object.entries(configQuery.data).map(([key, value]) => (
              <div key={key}>
                <dt className="text-xs text-gray-500">{key}</dt>
                <dd className="font-mono">{String(value)}</dd>
              </div>
            ))}
          </dl>
        )}
      </Card>

      <Card className="md:col-span-2">
        <CardHeader>
          <CardTitle>Recent risk decisions</CardTitle>
        </CardHeader>
        {decisionsQuery.isLoading && <SkeletonRows rows={4} />}
        {decisionsQuery.data && decisionsQuery.data.length === 0 && (
          <p className="text-sm text-gray-500">No risk decisions recorded yet.</p>
        )}
        {decisionsQuery.data && decisionsQuery.data.length > 0 && (
          <ul className="space-y-2 text-sm">
            {decisionsQuery.data.map((d) => (
              <li key={d.id} className="border-b border-gray-100 pb-2 dark:border-gray-800">
                <div className="flex items-center justify-between">
                  <span>
                    {d.strategy_id} — {new Date(d.bar_time).toLocaleString()}
                  </span>
                  {d.rejection_reason && <Badge tone="critical">{d.rejection_reason}</Badge>}
                </div>
                <div className="mt-1 flex flex-wrap gap-1">
                  {d.layer_results.map((lr, i) => (
                    <Badge key={i} tone={lr.passed ? "neutral" : "warning"}>
                      {lr.layer_name}: {lr.multiplier}
                      {lr.reason ? ` (${lr.reason})` : ""}
                    </Badge>
                  ))}
                </div>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <ArmingControl />

      <ConfirmDialog
        open={dialog === "engage"}
        title="Engage kill switch"
        danger
        confirmLabel="Engage"
        description={
          <div>
            <p className="mb-2">
              This blocks every new trade across the account until manually disengaged. Existing
              positions are NOT auto-flattened.
            </p>
            <input
              data-testid="kill-switch-engage-reason"
              className="w-full rounded border border-gray-300 px-2 py-1 text-sm dark:border-gray-700 dark:bg-gray-800"
              placeholder="Reason (required)"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
          </div>
        }
        onConfirm={async () => {
          await api.post("/api/risk/kill-switch/engage", { reason });
          await refetchKillSwitch();
        }}
        onClose={() => setDialog(null)}
      />

      <ConfirmDialog
        open={dialog === "disengage"}
        title="Disengage kill switch"
        confirmLabel="Disengage"
        description="Trading will resume being permitted account-wide. This requires the same manual re-confirmation the backend enforces — nothing here makes it easier than the API allows."
        onConfirm={async () => {
          await api.post("/api/risk/kill-switch/disengage");
          await refetchKillSwitch();
        }}
        onClose={() => setDialog(null)}
      />
    </div>
  );
}

function ArmingControl() {
  const queryClient = useQueryClient();
  const [strategyId, setStrategyId] = useState("");
  const [exchange, setExchange] = useState("binance");
  const [enabled, setEnabled] = useState(false);
  const [dialog, setDialog] = useState<"arm" | "disarm" | null>(null);
  const [reason, setReason] = useState("");

  const armingQuery = useQuery({
    queryKey: ["risk", "arming", strategyId, exchange],
    queryFn: () => api.get<ArmingStateOut>("/api/risk/arming", { strategy_id: strategyId, exchange }),
    enabled,
    retry: false,
  });

  async function refetchArming() {
    setEnabled(true);
    await queryClient.invalidateQueries({ queryKey: ["risk", "arming", strategyId, exchange] });
  }

  return (
    <Card className="md:col-span-2">
      <CardHeader>
        <CardTitle>Strategy arming</CardTitle>
      </CardHeader>
      <div className="mb-3 flex gap-2 text-sm">
        <input
          data-testid="arming-strategy-id"
          placeholder="strategy_id"
          className="rounded border border-gray-300 px-2 py-1 dark:border-gray-700 dark:bg-gray-800"
          value={strategyId}
          onChange={(e) => setStrategyId(e.target.value)}
        />
        <input
          data-testid="arming-exchange"
          placeholder="exchange"
          className="rounded border border-gray-300 px-2 py-1 dark:border-gray-700 dark:bg-gray-800"
          value={exchange}
          onChange={(e) => setExchange(e.target.value)}
        />
        <Button
          variant="secondary"
          onClick={() => setEnabled(true)}
          disabled={!strategyId}
          data-testid="arming-lookup"
        >
          Look up
        </Button>
      </div>
      {armingQuery.isError &&
        (armingQuery.error instanceof ApiError && armingQuery.error.status === 404 ? (
          <p className="text-sm text-gray-500">No arming record for this strategy/exchange.</p>
        ) : (
          <p className="text-sm text-red-600">Lookup failed.</p>
        ))}
      {armingQuery.data && (
        <div className="mb-2 text-sm">
          <Badge tone={armingQuery.data.armed ? "success" : "neutral"} data-testid="arming-status">
            {armingQuery.data.armed ? "Armed" : "Not armed"}
          </Badge>
          {armingQuery.data.expires_at && (
            <p className="mt-1 text-gray-500">
              Expires {new Date(armingQuery.data.expires_at).toLocaleString()}
            </p>
          )}
        </div>
      )}
      <div className="flex gap-2">
        <Button
          variant="primary"
          disabled={!strategyId}
          onClick={() => setDialog("arm")}
          data-testid="arming-arm-open"
        >
          Arm
        </Button>
        <Button
          variant="secondary"
          disabled={!strategyId}
          onClick={() => {
            setReason("");
            setDialog("disarm");
          }}
          data-testid="arming-disarm-open"
        >
          Disarm
        </Button>
      </div>

      <ConfirmDialog
        open={dialog === "arm"}
        title={`Arm ${strategyId || "strategy"} on ${exchange}`}
        confirmLabel="Arm"
        description="This authorizes live trading for this strategy/exchange for 48 hours. Testnet/paper only — mainnet arming is rejected by the backend (no cloud KMS is configured in this deployment)."
        onConfirm={async () => {
          await api.post("/api/risk/arming/arm", { strategy_id: strategyId, exchange, mainnet: false });
          await refetchArming();
        }}
        onClose={() => setDialog(null)}
      />

      <ConfirmDialog
        open={dialog === "disarm"}
        title={`Disarm ${strategyId || "strategy"} on ${exchange}`}
        confirmLabel="Disarm"
        description={
          <div>
            <p className="mb-2">This immediately revokes live-trading authorization.</p>
            <input
              data-testid="arming-disarm-reason"
              className="w-full rounded border border-gray-300 px-2 py-1 text-sm dark:border-gray-700 dark:bg-gray-800"
              placeholder="Reason (required)"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
          </div>
        }
        onConfirm={async () => {
          await api.post("/api/risk/arming/disarm", { strategy_id: strategyId, exchange, reason });
          await refetchArming();
        }}
        onClose={() => setDialog(null)}
      />
    </Card>
  );
}
