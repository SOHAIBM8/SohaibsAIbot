import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { createChart, CandlestickSeries, type IChartApi, type ISeriesApi } from "lightweight-charts";
import { api } from "@/lib/api";
import { Card, CardHeader, CardTitle } from "@/components/ui/Card";
import { SkeletonRows } from "@/components/ui/Skeleton";
import type { CandleOut } from "@/types/api";

/**
 * Strictly read-only (spec section 9) — no order entry lives here.
 * A "current regime" badge overlay is NOT shown: researched before
 * building (Step 7's own findings) — RegimeDetector is stateful/
 * in-memory-only and nothing persists a "current regime" value
 * anywhere, so there is no honest data source for that badge today.
 */
export function LiveMarketPage() {
  const [exchange, setExchange] = useState("binance");
  const [symbol, setSymbol] = useState("BTC/USDT");
  const [timeframe, setTimeframe] = useState("1h");
  const chartContainerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);

  const { data, isLoading } = useQuery({
    queryKey: ["market", "candles", exchange, symbol, timeframe],
    queryFn: () =>
      api.get<CandleOut[]>("/api/market/candles", { exchange, symbol, timeframe, limit: 300 }),
  });

  useEffect(() => {
    if (!chartContainerRef.current || chartRef.current) return;
    const chart = createChart(chartContainerRef.current, {
      height: 360,
      layout: { textColor: "#6b7280" },
      grid: { vertLines: { visible: false }, horzLines: { color: "#e5e7eb" } },
    });
    seriesRef.current = chart.addSeries(CandlestickSeries);
    chartRef.current = chart;

    const resizeObserver = new ResizeObserver((entries) => {
      const width = entries[0]?.contentRect.width;
      if (width) chart.applyOptions({ width });
    });
    resizeObserver.observe(chartContainerRef.current);

    return () => {
      resizeObserver.disconnect();
      chart.remove();
      chartRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (!seriesRef.current || !data) return;
    seriesRef.current.setData(
      data.map((c) => ({
        time: (new Date(c.open_time).getTime() / 1000) as import("lightweight-charts").UTCTimestamp,
        open: c.open,
        high: c.high,
        low: c.low,
        close: c.close,
      })),
    );
  }, [data]);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Live market</CardTitle>
        <div className="flex gap-2 text-sm">
          <input
            className="w-28 rounded border border-gray-300 px-2 py-1 dark:border-gray-700 dark:bg-gray-800"
            value={exchange}
            onChange={(e) => setExchange(e.target.value)}
          />
          <input
            className="w-28 rounded border border-gray-300 px-2 py-1 dark:border-gray-700 dark:bg-gray-800"
            value={symbol}
            onChange={(e) => setSymbol(e.target.value)}
          />
          <select
            className="rounded border border-gray-300 px-2 py-1 dark:border-gray-700 dark:bg-gray-800"
            value={timeframe}
            onChange={(e) => setTimeframe(e.target.value)}
          >
            {["1m", "5m", "15m", "1h", "4h", "1d"].map((tf) => (
              <option key={tf} value={tf}>
                {tf}
              </option>
            ))}
          </select>
        </div>
      </CardHeader>
      {isLoading && <SkeletonRows rows={4} />}
      {!isLoading && data && data.length === 0 && (
        <p className="text-sm text-gray-500">No candles ingested yet for this symbol/timeframe.</p>
      )}
      <div ref={chartContainerRef} className="w-full" />
    </Card>
  );
}
