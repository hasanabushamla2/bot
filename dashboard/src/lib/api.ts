const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

async function fetchAPI<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`API ${path}: ${res.status}`);
  return res.json();
}

export interface Health {
  status: string;
  uptime_seconds: number;
  engine_running: boolean;
  engine_state: string;
  live_trading: boolean;
  mode: string;
  heartbeat_age_seconds: number | null;
  session: {
    session_id: string;
    status: string;
    started_at: string;
    commit_sha: string;
  };
  database: {
    path: string;
    filename: string;
    exists: boolean;
    size_mb: number;
    last_modified: string | null;
  };
}

export interface Metrics {
  events: number;
  consumed: number;
  signals: number;
  opportunities: number;
  risk_assessments: number;
  orders: number;
  fills: number;
  equity: number;
  cash: number;
  realized_pnl: number;
  trade_count: number;
  win_count: number;
  loss_count: number;
}

export interface Position {
  symbol: string;
  direction: string;
  quantity: number;
  entry_price: number;
  entry_notional: number;
  stop_loss_price: number;
  strategy_id: string;
  trail_peak: number;
  trail_activated: boolean;
  opened_at: string;
}

export interface Trade {
  trade_id: string;
  symbol: string;
  direction: string;
  entry_price: number;
  exit_price: number;
  quantity: number;
  gross_pnl: number;
  fees: number;
  slippage_cost: number;
  net_pnl: number;
  return_pct: number;
  exit_reason: string;
  strategy_id: string;
  entry_time: string;
  exit_time: string;
}

export interface Risk {
  cash: number;
  equity: number;
  allocated: number;
  realized_pnl: number;
  total_fees: number;
  total_slippage: number;
  trade_count: number;
  peak_equity: number;
  max_drawdown_pct: number;
  exposure: number;
  per_market: Record<string, number>;
  per_strategy: Record<string, number>;
  consecutive_losses: number;
  breaker_active: boolean;
}

export interface SystemInfo {
  memory: { rss_mb: number };
  database: { path: string; size_bytes: number; size_mb: number; exists: boolean };
  heartbeat: { file_exists: boolean; age_seconds: number | null; healthy: boolean };
  lease: { owner_id: string; acquired_at: string; heartbeat_at: string; expires_at: string; active: boolean };
  feed_health: { status: string; note: string };
  uptime_seconds: number;
}

export interface LogEntry {
  timestamp: string;
  level: string;
  event: string;
  message: string;
}

export async function getHealth(): Promise<Health> {
  return fetchAPI<Health>("/health");
}

export async function getMetrics(): Promise<Metrics> {
  return fetchAPI<Metrics>("/metrics");
}

export async function getPositions(): Promise<{ positions: Position[]; count: number }> {
  return fetchAPI<{ positions: Position[]; count: number }>("/positions");
}

export async function getTrades(): Promise<{ trades: Trade[]; count: number }> {
  return fetchAPI<{ trades: Trade[]; count: number }>("/trades");
}

export async function getRisk(): Promise<Risk> {
  return fetchAPI<Risk>("/risk");
}

export async function getSystem(): Promise<SystemInfo> {
  return fetchAPI<SystemInfo>("/system");
}

export async function getLogs(): Promise<{ logs: LogEntry[]; count: number }> {
  return fetchAPI<{ logs: LogEntry[]; count: number }>("/logs");
}
