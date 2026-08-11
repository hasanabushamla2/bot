"use client";

import { useEffect, useState } from "react";
import { getPositions, type Position } from "@/lib/api";
import { formatUSD, formatNumber } from "@/lib/utils";

export default function PositionsPage() {
  const [positions, setPositions] = useState<Position[]>([]);
  const [count, setCount] = useState(0);

  useEffect(() => {
    let active = true;
    async function load() {
      try {
        const d = await getPositions();
        if (active) { setPositions(d.positions); setCount(d.count); }
      } catch {}
    }
    load();
    const iv = setInterval(load, 5000);
    return () => { active = false; clearInterval(iv); };
  }, []);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-lg font-semibold">Positions</h1>
        <p className="text-sm text-muted-foreground mt-1">{count} open position{count !== 1 ? "s" : ""}</p>
      </div>

      <div className="bg-card border border-border rounded-lg overflow-hidden">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left">
              <th className="px-4 py-3 text-xs text-muted-foreground uppercase tracking-wider font-medium">Symbol</th>
              <th className="px-4 py-3 text-xs text-muted-foreground uppercase tracking-wider font-medium">Qty</th>
              <th className="px-4 py-3 text-xs text-muted-foreground uppercase tracking-wider font-medium">Entry</th>
              <th className="px-4 py-3 text-xs text-muted-foreground uppercase tracking-wider font-medium">Notional</th>
              <th className="px-4 py-3 text-xs text-muted-foreground uppercase tracking-wider font-medium">Stop</th>
              <th className="px-4 py-3 text-xs text-muted-foreground uppercase tracking-wider font-medium">Trail</th>
              <th className="px-4 py-3 text-xs text-muted-foreground uppercase tracking-wider font-medium">Strategy</th>
            </tr>
          </thead>
          <tbody>
            {positions.length === 0 ? (
              <tr>
                <td colSpan={7} className="px-4 py-8 text-center text-muted-foreground">
                  No open positions
                </td>
              </tr>
            ) : (
              positions.map((p, i) => (
                <tr key={i} className="border-b border-border/50 hover:bg-secondary/30">
                  <td className="px-4 py-3 font-mono-tabular">{p.symbol}</td>
                  <td className="px-4 py-3 font-mono-tabular">{formatNumber(p.quantity)}</td>
                  <td className="px-4 py-3 font-mono-tabular">{formatUSD(p.entry_price)}</td>
                  <td className="px-4 py-3 font-mono-tabular">{formatUSD(p.entry_notional)}</td>
                  <td className="px-4 py-3 font-mono-tabular">{formatUSD(p.stop_loss_price)}</td>
                  <td className="px-4 py-3 font-mono-tabular">
                    {p.trail_activated ? (
                      <span className="text-positive">{formatUSD(p.trail_peak)}</span>
                    ) : (
                      <span className="text-muted-foreground">-</span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-xs text-muted-foreground">{p.strategy_id}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
