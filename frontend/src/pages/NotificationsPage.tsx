import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { Card, CardHeader, CardTitle } from "@/components/ui/Card";
import { SkeletonRows } from "@/components/ui/Skeleton";
import { Badge, severityToTone } from "@/components/ui/Badge";
import type { NotificationOut } from "@/types/api";

const SEVERITIES = ["", "critical", "warning", "info"] as const;

export function NotificationsPage() {
  const [severity, setSeverity] = useState<(typeof SEVERITIES)[number]>("");

  const { data, isLoading } = useQuery({
    queryKey: ["notifications", severity],
    queryFn: () =>
      api.get<NotificationOut[]>("/api/notifications", {
        severity: severity || undefined,
        limit: 100,
      }),
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle>Notifications</CardTitle>
        <select
          className="rounded border border-gray-300 px-2 py-1 text-sm dark:border-gray-700 dark:bg-gray-800"
          value={severity}
          onChange={(e) => setSeverity(e.target.value as (typeof SEVERITIES)[number])}
        >
          <option value="">All severities</option>
          <option value="critical">Critical</option>
          <option value="warning">Warning</option>
          <option value="info">Info</option>
        </select>
      </CardHeader>
      {isLoading && <SkeletonRows rows={5} />}
      {data && data.length === 0 && <p className="text-sm text-gray-500">No notifications yet.</p>}
      {data && data.length > 0 && (
        <ul className="space-y-2">
          {data.map((n) => (
            <li
              key={n.id}
              className="flex items-start justify-between border-b border-gray-100 pb-2 text-sm dark:border-gray-800"
            >
              <div>
                <Badge tone={severityToTone(n.severity)}>{n.severity}</Badge>
                <span className="ml-2">{n.message}</span>
              </div>
              <span className="whitespace-nowrap text-xs text-gray-400">
                {new Date(n.occurred_at).toLocaleString()}
              </span>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
