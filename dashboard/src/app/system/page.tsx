"use client";

import { useEffect, useState } from "react";
import { getSystem, type SystemInfo } from "@/lib/api";
import { formatDuration, timeAgo } from "@/lib/utils";

export default function SystemPage() {
  const [sys, setSys] = useState<SystemInfo | null>(null);

  useEffect(() => {
    let active = true;
    async function load() {
      try { const s = await getSystem(); if (active) setSys(s); } catch {}
    }
    load();
    const iv = setInterval(load, 5000);
    return () => { active = false; clearInterval(iv); };
  }, []);

  const Row = ({ label, value, accent }: { label: string; value: string | number | boolean; accent?: "positive" | "negative" | "warning" }) => {
    const color = accent === "positive" ? "hsl(160 60% 45%)" : accent === "negative" ? "hsl(0 62% 50%)" : accent === "warning" ? "hsl(40 80% 50%)" : undefined;
    return (
      <div className="flex justify-between items-center py-3 border-b border-border last:border-0">
        <span className="text-sm text-muted-foreground">{label}</span>
        <span className="text-sm font-mono-tabular" style={{ color }}>{String(value)}</span>
      </div>
    );
  };

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-lg font-semibold">System</h1>
        <p className="text-sm text-muted-foreground mt-1">Infrastructure health and status</p>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Memory */}
        <div className="bg-card border border-border rounded-lg p-5">
          <h2 className="text-sm font-medium mb-3">Memory</h2>
          <Row label="RSS" value={`${sys?.memory.rss_mb.toFixed(1) ?? "-"} MB`} />
          <Row label="Uptime" value={sys ? formatDuration(sys.uptime_seconds) : "-"} />
        </div>

        {/* Database */}
        <div className="bg-card border border-border rounded-lg p-5">
          <h2 className="text-sm font-medium mb-3">Database</h2>
          <Row label="Path" value={sys?.database.path ?? "-"} />
          <Row label="Size" value={sys?.database.size_mb !== undefined ? `${sys.database.size_mb} MB` : "-"} />
          <Row label="Exists" value={sys?.database.exists ? "Yes" : "No"} accent={sys?.database.exists ? "positive" : "negative"} />
        </div>

        {/* Heartbeat */}
        <div className="bg-card border border-border rounded-lg p-5">
          <h2 className="text-sm font-medium mb-3">Heartbeat</h2>
          <Row label="File exists" value={sys?.heartbeat.file_exists ? "Yes" : "No"} accent={sys?.heartbeat.file_exists ? "positive" : "negative"} />
          <Row label="Age" value={sys?.heartbeat.age_seconds != null ? `${sys.heartbeat.age_seconds.toFixed(1)}s` : "None"} />
          <Row label="Healthy" value={sys?.heartbeat.healthy ? "Yes" : "No"} accent={sys?.heartbeat.healthy ? "positive" : "negative"} />
        </div>

        {/* Lease */}
        <div className="bg-card border border-border rounded-lg p-5">
          <h2 className="text-sm font-medium mb-3">Runtime Lease</h2>
          <Row label="Active" value={sys?.lease.active ? "Yes" : "No"} accent={sys?.lease.active ? "positive" : "negative"} />
          <Row label="Owner" value={sys?.lease.owner_id ? sys.lease.owner_id.slice(0, 20) + "..." : "-"} />
          <Row label="Acquired" value={sys?.lease.acquired_at ? timeAgo(sys.lease.acquired_at) : "-"} />
          <Row label="Heartbeat" value={sys?.lease.heartbeat_at ? timeAgo(sys.lease.heartbeat_at) : "-"} />
        </div>
      </div>
    </div>
  );
}
