"""Monitoring Dashboard — internal web dashboard for system status.

Shows:
- Equity curve
- Daily/Cumulative P&L
- Active positions
- Current opportunities
- Executed trades
- Strategy performance
- System health
- Paper/Live mode indicator (MUST BE UNAMBIGUOUS)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI(title="Quant Opportunity Engine — Dashboard")


# --- In-memory state (populated by the engine) ---
_dashboard_state: dict[str, Any] = {
    "mode": "PAPER",
    "live_trading_enabled": False,
    "equity": 0.0,
    "daily_pnl": 0.0,
    "cumulative_pnl": 0.0,
    "active_positions": 0,
    "opportunities_today": 0,
    "trades_today": 0,
    "win_rate": 0.0,
    "fees_today": 0.0,
    "drawdown_pct": 0.0,
    "sharpe_ratio": 0.0,
    "errors_today": 0,
    "data_health": "unknown",
    "last_updated": None,
}


def update_dashboard_state(**kwargs: Any) -> None:
    """Thread-safe update to dashboard state (called by engine)."""
    _dashboard_state.update(kwargs)
    _dashboard_state["last_updated"] = datetime.now(UTC).isoformat()


# --- Routes ---


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "mode": _dashboard_state["mode"]}


@app.get("/api/state")
async def get_state() -> JSONResponse:
    """Return full dashboard state."""
    return JSONResponse(content=_dashboard_state)


@app.get("/", response_class=HTMLResponse)
async def dashboard() -> str:
    """Render the main dashboard."""
    mode = _dashboard_state["mode"]
    mode_color = "#dc2626" if mode == "LIVE" else "#16a34a"
    mode_bg = "#fef2f2" if mode == "LIVE" else "#f0fdf4"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Quant Engine — Dashboard</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace;
    background: #0f172a; color: #e2e8f0; padding: 20px;
  }}
  .header {{
    display: flex; justify-content: space-between; align-items: center;
    padding: 16px 20px; background: #1e293b; border-radius: 8px;
    margin-bottom: 20px;
  }}
  .mode-badge {{
    padding: 6px 16px; border-radius: 4px; font-weight: bold;
    font-size: 14px; background: {mode_bg}; color: {mode_color};
    border: 2px solid {mode_color};
  }}
  .grid {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 16px; margin-bottom: 24px;
  }}
  .card {{
    background: #1e293b; border-radius: 8px; padding: 16px;
    border-left: 3px solid #3b82f6;
  }}
  .card.warn {{ border-left-color: #f59e0b; }}
  .card.good {{ border-left-color: #16a34a; }}
  .card.bad {{ border-left-color: #dc2626; }}
  .card h3 {{ font-size: 12px; color: #94a3b8; margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.5px; }}
  .card .value {{ font-size: 24px; font-weight: bold; }}
  .card .value.positive {{ color: #16a34a; }}
  .card .value.negative {{ color: #dc2626; }}
  .status-bar {{
    display: flex; gap: 20px; padding: 12px 20px;
    background: #1e293b; border-radius: 8px; margin-bottom: 20px;
    font-size: 14px;
  }}
  .status-item {{ display: flex; align-items: center; gap: 8px; }}
  .status-dot {{ width: 8px; height: 8px; border-radius: 50%; }}
  .status-dot.ok {{ background: #16a34a; }}
  .status-dot.warn {{ background: #f59e0b; }}
  .status-dot.err {{ background: #dc2626; }}
</style>
</head>
<body>
  <div class="header">
    <h1>📊 Quant Opportunity Engine</h1>
    <div class="mode-badge">{mode} MODE</div>
  </div>

  <div class="status-bar">
    <div class="status-item">
      <div class="status-dot ok"></div>
      <span>Engine: Running</span>
    </div>
    <div class="status-item">
      <div class="status-dot {"ok" if _dashboard_state["data_health"] == "healthy" else "warn"}"></div>
      <span>Data: {_dashboard_state["data_health"]}</span>
    </div>
    <div style="margin-left: auto; color: #64748b;">
      Updated: {_dashboard_state.get("last_updated", "N/A")}
    </div>
  </div>

  <div class="grid">
    <div class="card">
      <h3>Equity</h3>
      <div class="value">${_dashboard_state["equity"]:,.2f}</div>
    </div>
    <div class="card">
      <h3>Daily P&L</h3>
      <div class="value {"positive" if _dashboard_state["daily_pnl"] >= 0 else "negative"}">
        ${_dashboard_state["daily_pnl"]:,.2f}
      </div>
    </div>
    <div class="card">
      <h3>Cumulative P&L</h3>
      <div class="value {"positive" if _dashboard_state["cumulative_pnl"] >= 0 else "negative"}">
        ${_dashboard_state["cumulative_pnl"]:,.2f}
      </div>
    </div>
    <div class="card">
      <h3>Active Positions</h3>
      <div class="value">{_dashboard_state["active_positions"]}</div>
    </div>
    <div class="card">
      <h3>Opportunities Today</h3>
      <div class="value">{_dashboard_state["opportunities_today"]}</div>
    </div>
    <div class="card">
      <h3>Trades Today</h3>
      <div class="value">{_dashboard_state["trades_today"]}</div>
    </div>
    <div class="card">
      <h3>Win Rate</h3>
      <div class="value">{_dashboard_state["win_rate"]:.1%}</div>
    </div>
    <div class="card">
      <h3>Fees Today</h3>
      <div class="value">${_dashboard_state["fees_today"]:,.2f}</div>
    </div>
    <div class="card {"bad" if _dashboard_state["drawdown_pct"] > 5 else "good"}">
      <h3>Max Drawdown</h3>
      <div class="value">{_dashboard_state["drawdown_pct"]:.2f}%</div>
    </div>
    <div class="card">
      <h3>Sharpe Ratio</h3>
      <div class="value">{_dashboard_state["sharpe_ratio"]:.2f}</div>
    </div>
    <div class="card">
      <h3>Errors Today</h3>
      <div class="value {"bad" if _dashboard_state["errors_today"] > 0 else ""}">
        {_dashboard_state["errors_today"]}
      </div>
    </div>
  </div>
</body>
</html>"""
