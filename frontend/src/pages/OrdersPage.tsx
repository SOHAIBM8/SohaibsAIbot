import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "@/lib/api";
import { Card, CardHeader, CardTitle } from "@/components/ui/Card";
import { Table, Thead, Tbody, Th, Td } from "@/components/ui/Table";
import { SkeletonRows } from "@/components/ui/Skeleton";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import type { OrderOut } from "@/types/api";

const STATE_TONE: Record<string, "success" | "warning" | "critical" | "neutral"> = {
  filled: "success",
  submitted: "warning",
  partially_filled: "warning",
  pending: "neutral",
  rejected: "critical",
  cancelled: "neutral",
  pending_cancel: "warning",
};

const CANCELLABLE_STATES = new Set(["submitted", "partially_filled", "pending_cancel"]);

export function OrdersPage() {
  const queryClient = useQueryClient();
  const [symbol, setSymbol] = useState("");
  const [state, setState] = useState("");
  const [cancelTarget, setCancelTarget] = useState<string | null>(null);

  const { data, isLoading } = useQuery({
    queryKey: ["orders", { symbol, state }],
    queryFn: () =>
      api.get<OrderOut[]>("/api/orders", {
        symbol: symbol || undefined,
        state: state || undefined,
        limit: 100,
      }),
  });

  async function refetchOrders() {
    await queryClient.invalidateQueries({ queryKey: ["orders"] });
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Orders</CardTitle>
        <div className="flex gap-2 text-sm">
          <input
            placeholder="Filter symbol"
            className="rounded border border-gray-300 px-2 py-1 dark:border-gray-700 dark:bg-gray-800"
            value={symbol}
            onChange={(e) => setSymbol(e.target.value)}
          />
          <input
            placeholder="Filter state"
            className="rounded border border-gray-300 px-2 py-1 dark:border-gray-700 dark:bg-gray-800"
            value={state}
            onChange={(e) => setState(e.target.value)}
          />
        </div>
      </CardHeader>
      {isLoading && <SkeletonRows rows={5} />}
      {data && data.length === 0 && <p className="text-sm text-gray-500">No orders found.</p>}
      {data && data.length > 0 && (
        <Table>
          <Thead>
            <tr>
              <Th>Order</Th>
              <Th>Strategy</Th>
              <Th>Symbol</Th>
              <Th>Direction</Th>
              <Th>Quantity</Th>
              <Th>Mode</Th>
              <Th>State</Th>
              <Th>Created</Th>
              <Th>Action</Th>
            </tr>
          </Thead>
          <Tbody>
            {data.map((o) => (
              <tr key={o.client_order_id}>
                <Td>
                  <Link
                    to={`/orders/${o.client_order_id}`}
                    className="text-blue-600 hover:underline dark:text-blue-400"
                  >
                    {o.client_order_id.slice(0, 12)}…
                  </Link>
                </Td>
                <Td>{o.strategy_id}</Td>
                <Td>{o.symbol}</Td>
                <Td>{o.direction === 1 ? "Long" : "Short"}</Td>
                <Td>{o.quantity}</Td>
                <Td>{o.mode}</Td>
                <Td>
                  <Badge tone={STATE_TONE[o.state] ?? "neutral"}>{o.state}</Badge>
                </Td>
                <Td>{new Date(o.created_at).toLocaleString()}</Td>
                <Td>
                  {CANCELLABLE_STATES.has(o.state) && (
                    <Button variant="danger" onClick={() => setCancelTarget(o.client_order_id)}>
                      Cancel
                    </Button>
                  )}
                </Td>
              </tr>
            ))}
          </Tbody>
        </Table>
      )}

      <ConfirmDialog
        open={cancelTarget !== null}
        title="Cancel order"
        danger
        confirmLabel="Cancel order"
        description={`This cancels order ${cancelTarget ?? ""}. Live-order cancellation is not supported by this build (paper orders only).`}
        onConfirm={async () => {
          await api.post(`/api/orders/${cancelTarget}/cancel`);
          await refetchOrders();
        }}
        onClose={() => setCancelTarget(null)}
      />
    </Card>
  );
}
