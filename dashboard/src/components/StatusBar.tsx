"use client";

import { useEffect, useState } from "react";
import { getHealth, type Health } from "@/lib/api";
import { Circle, Database, Activity } from "lucide-react";

export function StatusBar() {
  const [health, setHealth] = useState<Health | null>(null);

  useEffect(() => {
    let active = true;
    async function poll() {
      try {
        const h = await getHealth();
        if (active) setHealth(h);
      } catch {
        if (active) setHealth(null);
      }
    }
    poll();
    const iv = setInterval(poll, 5000);
    return () => {
      active = false;
      clearInterval(iv);
    };
  }, []);

  const ok = health?.engine_running && health?.status === "healthy";

  return (
    <div className="h-9 flex items-center gap-6 px-6 border-b border-border bg-card/50 text-xs shrink-0">
      <div className="flex items-center gap-2">
        <Circle
          className="w-2 h-2 fill-current"
          style={{ color: ok ? "hsl(160 60% 45%)" : "hsl(0 62% 50%)" }}
        />
        <span className="text-muted-foreground">Engine:</span>
        <span className={ok ? "text-positive" : "text-negative"}>
          {health ? (ok ? "RUNNING" : "STOPPED") : "OFFLINE"}
        </span>
      </div>

      <div className="flex items-center gap-2">
        <Activity className="w-3 h-3 text-muted-foreground" />
        <span className="text-muted-foreground">Mode:</span>
        <span className="text-foreground font-medium">PAPER</span>
      </div>

      <div className="flex items-center gap-2">
        <span className="w-2 h-2 rounded-full bg-negative" />
        <span className="text-muted-foreground">Live Trading:</span>
        <span className="text-negative font-medium">DISABLED</span>
      </div>

      <div className="ml-auto flex items-center gap-2">
        <Database className="w-3 h-3 text-muted-foreground" />
        <span className="text-muted-foreground">
          {health?.db_exists ? "DB Connected" : "DB Offline"}
        </span>
      </div>
    </div>
  );
}
