"use client";

import { useEffect, useState } from "react";
import { getHealth, getMetrics, type Health, type Metrics } from "@/lib/api";
import { formatUSD, formatNumber, formatDuration } from "@/lib/utils";
import {
  Activity,
  TrendingUp,
  CircleDollarSign,
  BarChart3,
  ArrowUpRight,
  ArrowDownRight,
} from "lucide-react";

function StatCard({
  label,
  value,
  sub,
  icon: Icon,
  accent,
}: {
  label: string;
  value: string;
  sub?: string;
  icon: React.ComponentType<{ className?: string }>;
  accent?: "positive" | "negative";
}) {
  return (
    <div className="bg-card border border-border rounded-lg p-4">
      <div className="flex items-center justify-between mb-2">
        <span className="text-xs text-muted-foreground uppercase tracking-wider">{label}</span>
        <Icon className="w-4 h-4 text-muted-foreground" />
      </div>
      <div
        className="text-2xl font-mono-tabular font-semibold"
        style={{
          color:
            accent === "positive"
              ? "hsl(160 60% 45%)"
              : accent === "negative"
              ? "hsl(0 62% 50%)"
              : undefined,
        }}
      >
        {value}
      </div>
      {sub && <div className="text-xs text-muted-foreground mt-1">{sub}</div>}
    </div>
  );
}

export default function OverviewPage() {
  const [health, setHealth] = useState<Health | null>(null);
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    async function load() {
      try {
        const [h, m] = await Promise.all([getHealth(), getMetrics()]);
        if (active) {
          setHealth(h);
          setMetrics(m);
          setError(null);
        }
      } catch (e) {
        if (active) setError(String(e));
      }
    }
    load();
    const iv = setInterval(load, 5000);
    return () => {
      active = false;
      clearInterval(iv);
    };
  }, []);

  if (error) {
    return (
      <div className="flex items-center justify-center h-64 text-negative text-sm">
        API offline — {error}
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-lg font-semibold">Dashboard Overview</h1>
        <p className="text-sm text-muted-foreground mt-1">
          Paper trading engine — monitoring only
        </p>
      </div>

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard
          label="Engine Status"
          value={health?.engine_running ? "RUNNING" : "STOPPED"}
          sub={`Uptime: ${health ? formatDuration(health.uptime_seconds) : "-"}`}
          icon={Activity}
          accent={health?.engine_running ? "positive" : "negative"}
        />
        <StatCard
          label="Equity"
          value={metrics ? formatUSD(metrics.equity) : "-"}
          icon={CircleDollarSign}
        />
        <StatCard
          label="Realized PnL"
          value={metrics ? formatUSD(metrics.realized_pnl) : "-"}
          icon={TrendingUp}
          accent={metrics && metrics.realized_pnl >= 0 ? "positive" : "negative"}
        />
        <StatCard
          label="Total Trades"
          value={metrics ? formatNumber(metrics.trade_count) : "-"}
          sub={
            metrics
              ? `${metrics.win_count}W / ${metrics.loss_count}L`
              : undefined
          }
          icon={BarChart3}
        />
      </div>

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard
          label="Orders"
          value={metrics ? formatNumber(metrics.orders) : "-"}
          icon={ArrowUpRight}
        />
        <StatCard
          label="Fills"
          value={metrics ? formatNumber(metrics.fills) : "-"}
          icon={ArrowDownRight}
        />
        <StatCard
          label="Cash"
          value={metrics ? formatUSD(metrics.cash) : "-"}
          icon={CircleDollarSign}
        />
        <StatCard
          label="Live Trading"
          value="DISABLED"
          icon={Activity}
          accent="negative"
        />
      </div>
    </div>
  );
}
