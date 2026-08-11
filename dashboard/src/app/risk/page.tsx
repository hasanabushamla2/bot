"use client";

import { useEffect, useState } from "react";
import { getRisk, type Risk } from "@/lib/api";
import { formatUSD, formatPct } from "@/lib/utils";

export default function RiskPage() {
  const [risk, setRisk] = useState<Risk | null>(null);

  useEffect(() => {
    let active = true;
    async function load() {
      try { const r = await getRisk(); if (active) setRisk(r); } catch {}
    }
    load();
    const iv = setInterval(load, 5000);
    return () => { active = false; clearInterval(iv); };
  }, []);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-lg font-semibold">Risk &amp; Account</h1>
        <p className="text-sm text-muted-foreground mt-1">Account state and risk metrics</p>
      </div>

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        {[
          ["Cash", risk?.cash],
          ["Equity", risk?.equity],
          ["Allocated", risk?.allocated],
          ["Realized PnL", risk?.realized_pnl],
          ["Total Fees", risk?.total_fees],
          ["Total Slippage", risk?.total_slippage],
          ["Peak Equity", risk?.peak_equity],
          ["Max Drawdown", risk?.max_drawdown_pct !== undefined ? `${risk.max_drawdown_pct.toFixed(2)}%` : "-"],
        ].map(([label, val]) => (
          <div key={label} className="bg-card border border-border rounded-lg p-4">
            <div className="text-xs text-muted-foreground uppercase tracking-wider mb-1">{label}</div>
            <div className="text-xl font-mono-tabular font-semibold">
              {val !== undefined && val !== null
                ? typeof val === "number"
                  ? (val > 1000 ? formatUSD(val as number) : `${(val as number).toFixed(4)}`)
                  : val
                : "-"}
            </div>
          </div>
        ))}
      </div>

      <div className="bg-card border border-border rounded-lg p-6">
        <h2 className="text-sm font-medium mb-4">Risk State</h2>
        <div className="grid grid-cols-2 gap-4 text-sm">
          <div className="flex justify-between py-2 border-b border-border">
            <span className="text-muted-foreground">Exposure</span>
            <span className="font-mono-tabular">{risk ? formatUSD(risk.exposure) : "-"}</span>
          </div>
          <div className="flex justify-between py-2 border-b border-border">
            <span className="text-muted-foreground">Consecutive Losses</span>
            <span className="font-mono-tabular">{risk?.consecutive_losses ?? "-"}</span>
          </div>
          <div className="flex justify-between py-2 border-b border-border">
            <span className="text-muted-foreground">Circuit Breaker</span>
            <span
              className="font-mono-tabular font-medium"
              style={{ color: risk?.breaker_active ? "hsl(0 62% 50%)" : "hsl(160 60% 45%)" }}
            >
              {risk?.breaker_active ? "TRIPPED" : "NORMAL"}
            </span>
          </div>
          <div className="flex justify-between py-2 border-b border-border">
            <span className="text-muted-foreground">Trade Count</span>
            <span className="font-mono-tabular">{risk?.trade_count ?? "-"}</span>
          </div>
        </div>
      </div>
    </div>
  );
}
