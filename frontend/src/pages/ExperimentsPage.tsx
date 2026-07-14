import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { Card, CardHeader, CardTitle } from "@/components/ui/Card";
import { Table, Thead, Tbody, Th, Td } from "@/components/ui/Table";
import { SkeletonRows } from "@/components/ui/Skeleton";
import type { ComparisonTableOut, ExperimentResultOut } from "@/types/api";

export function ExperimentsPage() {
  const [selected, setSelected] = useState<number[]>([]);

  const listQuery = useQuery({
    queryKey: ["experiments"],
    queryFn: () => api.get<ExperimentResultOut[]>("/api/experiments", { limit: 50 }),
  });

  const compareQuery = useQuery({
    queryKey: ["experiments", "compare", selected],
    queryFn: () =>
      api.get<ComparisonTableOut>("/api/experiments/compare", { experiment_ids: selected.map(String) }),
    enabled: selected.length > 0,
  });

  function toggleSelected(id: number) {
    setSelected((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
  }

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle>Experiments</CardTitle>
        </CardHeader>
        {listQuery.isLoading && <SkeletonRows rows={5} />}
        {listQuery.data && listQuery.data.length === 0 && (
          <p className="text-sm text-gray-500">No experiments recorded yet.</p>
        )}
        {listQuery.data && listQuery.data.length > 0 && (
          <Table>
            <Thead>
              <tr>
                <Th>Compare</Th>
                <Th>ID</Th>
                <Th>Symbol</Th>
                <Th>Timeframe</Th>
                <Th>Started</Th>
                <Th>Sharpe</Th>
              </tr>
            </Thead>
            <Tbody>
              {listQuery.data.map((exp) => (
                <tr key={exp.experiment_id}>
                  <Td>
                    <input
                      type="checkbox"
                      checked={selected.includes(exp.experiment_id)}
                      onChange={() => toggleSelected(exp.experiment_id)}
                    />
                  </Td>
                  <Td>{exp.experiment_id}</Td>
                  <Td>{exp.config.symbol}</Td>
                  <Td>{exp.config.timeframe}</Td>
                  <Td>{new Date(exp.started_at).toLocaleDateString()}</Td>
                  <Td>{exp.metrics.sharpe !== undefined ? exp.metrics.sharpe.toFixed(2) : "—"}</Td>
                </tr>
              ))}
            </Tbody>
          </Table>
        )}
      </Card>

      {selected.length > 0 && compareQuery.data && (
        <Card>
          <CardHeader>
            <CardTitle>Comparison</CardTitle>
          </CardHeader>
          <Table>
            <Thead>
              <tr>
                <Th>ID</Th>
                <Th>Commit</Th>
                {Object.keys(compareQuery.data.results[0]?.metrics ?? {}).map((metric) => (
                  <Th key={metric}>{metric}</Th>
                ))}
              </tr>
            </Thead>
            <Tbody>
              {compareQuery.data.results.map((r) => (
                <tr key={r.experiment_id}>
                  <Td>{r.experiment_id}</Td>
                  <Td>{r.config.code_commit_hash.slice(0, 8)}</Td>
                  {Object.keys(compareQuery.data!.results[0]?.metrics ?? {}).map((metric) => (
                    <Td key={metric}>{r.metrics[metric] ?? "—"}</Td>
                  ))}
                </tr>
              ))}
            </Tbody>
          </Table>
        </Card>
      )}
    </div>
  );
}
