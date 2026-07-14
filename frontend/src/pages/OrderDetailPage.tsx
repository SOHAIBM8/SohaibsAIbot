import { useParams, Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { Card, CardHeader, CardTitle } from "@/components/ui/Card";
import { Table, Thead, Tbody, Th, Td } from "@/components/ui/Table";
import { SkeletonRows } from "@/components/ui/Skeleton";
import { Badge } from "@/components/ui/Badge";
import type { ExplanationOut, OrderDetailOut } from "@/types/api";

export function OrderDetailPage() {
  const { clientOrderId } = useParams<{ clientOrderId: string }>();

  const orderQuery = useQuery({
    queryKey: ["orders", clientOrderId],
    queryFn: () => api.get<OrderDetailOut>(`/api/orders/${clientOrderId}`),
    enabled: !!clientOrderId,
  });

  const explanationQuery = useQuery({
    queryKey: ["ai", "explanations", "trade", clientOrderId],
    queryFn: () => api.get<ExplanationOut>(`/api/ai/explanations/trade/${clientOrderId}`),
    enabled: false, // AI explanation generation costs a real LLM call — opt-in via the button below.
    retry: false,
  });

  if (orderQuery.isLoading || !orderQuery.data) return <SkeletonRows rows={4} />;
  const order = orderQuery.data;

  return (
    <div className="space-y-4">
      <Link to="/orders" className="text-sm text-blue-600 hover:underline dark:text-blue-400">
        ← Back to orders
      </Link>
      <Card>
        <CardHeader>
          <CardTitle>Order {order.client_order_id}</CardTitle>
        </CardHeader>
        <dl className="grid grid-cols-2 gap-2 text-sm">
          <div>
            <dt className="text-gray-500">Strategy</dt>
            <dd>{order.strategy_id}</dd>
          </div>
          <div>
            <dt className="text-gray-500">Symbol</dt>
            <dd>{order.symbol}</dd>
          </div>
          <div>
            <dt className="text-gray-500">State</dt>
            <dd>
              <Badge tone={order.state === "filled" ? "success" : "neutral"}>{order.state}</Badge>
            </dd>
          </div>
          <div>
            <dt className="text-gray-500">Mode</dt>
            <dd>{order.mode}</dd>
          </div>
          <div>
            <dt className="text-gray-500">Quantity</dt>
            <dd>{order.quantity}</dd>
          </div>
          <div>
            <dt className="text-gray-500">Direction</dt>
            <dd>{order.direction === 1 ? "Long" : "Short"}</dd>
          </div>
        </dl>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Fills</CardTitle>
        </CardHeader>
        {order.fills.length === 0 ? (
          <p className="text-sm text-gray-500">No fills yet.</p>
        ) : (
          <Table>
            <Thead>
              <tr>
                <Th>Price</Th>
                <Th>Quantity</Th>
                <Th>Fee</Th>
                <Th>Filled at</Th>
              </tr>
            </Thead>
            <Tbody>
              {order.fills.map((f) => (
                <tr key={f.id}>
                  <Td>{f.fill_price}</Td>
                  <Td>{f.quantity}</Td>
                  <Td>{f.fee}</Td>
                  <Td>{new Date(f.filled_at).toLocaleString()}</Td>
                </tr>
              ))}
            </Tbody>
          </Table>
        )}
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>AI explanation</CardTitle>
        </CardHeader>
        {!explanationQuery.data && !explanationQuery.isFetching && (
          <button
            className="rounded bg-blue-600 px-3 py-1.5 text-sm text-white hover:bg-blue-700"
            onClick={() => void explanationQuery.refetch()}
          >
            Generate explanation
          </button>
        )}
        {explanationQuery.isFetching && <p className="text-sm text-gray-500">Generating…</p>}
        {explanationQuery.data && (
          <div>
            <div className="mb-2">
              <Badge tone="info">AI-generated narration — not a system decision</Badge>
            </div>
            <p className="text-sm text-gray-700 dark:text-gray-300">
              {explanationQuery.data.generated_text}
            </p>
          </div>
        )}
      </Card>
    </div>
  );
}
