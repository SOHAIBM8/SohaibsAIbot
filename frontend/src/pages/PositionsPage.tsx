import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { Card, CardHeader, CardTitle } from "@/components/ui/Card";
import { UnavailableNotice } from "@/components/ui/UnavailableNotice";
import { SkeletonRows } from "@/components/ui/Skeleton";
import type { PositionsResponseOut } from "@/types/api";

export function PositionsPage() {
  const { data, isLoading } = useQuery({
    queryKey: ["positions"],
    queryFn: () => api.get<PositionsResponseOut>("/api/positions"),
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle>Positions</CardTitle>
      </CardHeader>
      {isLoading && <SkeletonRows rows={3} />}
      {data && !data.available && <UnavailableNotice reason={data.reason ?? ""} />}
    </Card>
  );
}
