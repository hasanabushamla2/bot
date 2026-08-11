"use client";

import { useEffect, useState } from "react";
import { getTrades, type Trade } from "@/lib/api";
import { formatUSD, formatPct, formatNumber } from "@/lib/utils";

export default function TradesPage() {
  const [trades, setTrades] = useState<Trade[]>([]);
  const [count, setCount] = useState(0);

  useEffect(() => {
    let active = true;
    async function load() {
      try {
        const d = await getTrades();
        if (active) { setTrades(d.trades); setCount(d.count); }
      } catch {}
    }
    load();
    const iv = setInterval(load, 5000);
    return () => { active = false; clearInterval(iv); };
  }, []);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-lg font-semibold">Trade History</h1>
        <p className="text-sm text-muted-foreground mt-1">{count} closed trades</p>
      </div>

      <div className="bg-card border border-border rounded-lg overflow-hidden overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left">
              <th className="px-4 py-3 text-xs text-muted-foreground uppercase tracking-wider font-medium">Symbol</th>
              <th className="px-4 py-3 text-xs text-muted-foreground uppercase tracking-wider font-medium">Qty</th>
              <th className="px-4 py-3 text-xs text-muted-foreground uppercase tracking-wider font-medium">Entry</th>
              <th className="px-4 py-3 text-xs text-muted-foreground uppercase tracking-wider font-medium">Exit</th>
              <th className="px-4 py-3 text-xs text-muted-foreground uppercase tracking-wider font-medium">PnL</th>
              <th className="px-4 py-3 text-xs text-muted-foreground uppercase tracking-wider font-medium">Return</th>
              <th className="px-4 py-3 text-xs text-muted-foreground uppercase tracking-wider font-medium">Fees</th>
              <th className="px-4 py-3 text-xs text-muted-foreground uppercase tracking-wider font-medium">Reason</th>
            </tr>
          </thead>
          <tbody>
            {trades.length === 0 ? (
              <tr>
                <td colSpan={8} className="px-4 py-8 text-center text-muted-foreground">
                  No closed trades
                </td>
              </tr>
            ) : (
              trades.slice(0, 100).map((t, i) => (
                <tr key={i} className="border-b border-border/50 hover:bg-secondary/30">
                  <td className="px-4 py-3 font-mono-tabular">{t.symbol}</td>
                  <td className="px-4 py-3 font-mono-tabular">{formatNumber(t.quantity)}</td>
                  <td className="px-4 py-3 font-mono-tabular">{formatUSD(t.entry_price)}</td>
                  <td className="px-4 py-3 font-mono-tabular">{formatUSD(t.exit_price)}</td>
                  <td className="px-4 py-3 font-mono-tabular">
                    <span style={{ color: t.net_pnl >= 0 ? "hsl(160 60% 45%)" : "hsl(0 62% 50%)" }}>
                      {formatUSD(t.net_pnl)}
                    </span>
                  </td>
                  <td className="px-4 py-3 font-mono-tabular">
                    <span style={{ color: t.return_pct >= 0 ? "hsl(160 60% 45%)" : "hsl(0 62% 50%)" }}>
                      {formatPct(t.return_pct)}
                    </span>
                  </td>
                  <td className="px-4 py-3 font-mono-tabular">{formatUSD(t.fees)}</td>
                  <td className="px-4 py-3 text-xs text-muted-foreground">{t.exit_reason}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
