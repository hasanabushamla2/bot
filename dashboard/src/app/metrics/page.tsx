"use client";

import { useEffect, useState } from "react";
import { getMetrics, type Metrics } from "@/lib/api";
import { formatNumber } from "@/lib/utils";
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from "recharts";

export default function MetricsPage() {
  const [metrics, setMetrics] = useState<Metrics | null>(null);

  useEffect(() => {
    let active = true;
    async function load() {
      try {
        const m = await getMetrics();
        if (active) setMetrics(m);
      } catch {}
    }
    load();
    const iv = setInterval(load, 5000);
    return () => { active = false; clearInterval(iv); };
  }, []);

  const chartData = metrics
    ? [
        { name: "Orders", value: metrics.orders },
        { name: "Fills", value: metrics.fills },
        { name: "Trades", value: metrics.trade_count },
      ]
    : [];

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-lg font-semibold">Metrics</h1>
        <p className="text-sm text-muted-foreground mt-1">Order and fill activity</p>
      </div>

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        {[
          ["Orders", metrics?.orders],
          ["Fills", metrics?.fills],
          ["Signals", 0],
          ["Opportunities", 0],
        ].map(([label, val]) => (
          <div key={label} className="bg-card border border-border rounded-lg p-4">
            <div className="text-xs text-muted-foreground uppercase tracking-wider mb-1">{label}</div>
            <div className="text-2xl font-mono-tabular font-semibold">
              {val !== undefined ? formatNumber(val as number) : "-"}
            </div>
          </div>
        ))}
      </div>

      <div className="bg-card border border-border rounded-lg p-6">
        <h2 className="text-sm font-medium mb-4">Execution Activity</h2>
        <div className="h-64">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={chartData}>
              <CartesianGrid strokeDasharray="3 3" stroke="hsl(220 13% 18%)" />
              <XAxis dataKey="name" stroke="hsl(220 13% 50%)" fontSize={12} />
              <YAxis stroke="hsl(220 13% 50%)" fontSize={12} />
              <Tooltip
                contentStyle={{
                  backgroundColor: "hsl(220 13% 10%)",
                  border: "1px solid hsl(220 13% 18%)",
                  borderRadius: "6px",
                  fontSize: "12px",
                }}
                labelStyle={{ color: "hsl(220 13% 70%)" }}
              />
              <Bar dataKey="value" fill="hsl(220 80% 60%)" radius={[4, 4, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>
    </div>
  );
}
