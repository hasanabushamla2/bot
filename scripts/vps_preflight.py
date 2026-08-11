#!/usr/bin/env python3
# ruff: noqa: T201
"""R9: VPS Preflight — PUBLIC DATA ONLY. No trading. No API keys."""

from __future__ import annotations

import json
import shutil
import socket
import ssl
import sys
import time
import urllib.request
from pathlib import Path

RESULTS = {}


def check(name, ok, detail=""):
    RESULTS[name] = ("PASS" if ok else "FAIL") + ": " + detail
    print(f"  [{'OK' if ok else 'FAIL'}] {name}: {detail}")


def main():
    print("=" * 60)
    print("  VPS PREFLIGHT — PUBLIC DATA ONLY")
    print("=" * 60)
    check("python", sys.version_info >= (3, 11), f"Python {sys.version.split()[0]}")
    try:
        a = socket.getaddrinfo("api.binance.com", 443)
        check("dns", True, f"api.binance.com -> {a[0][4][0]}")
    except Exception as e:
        check("dns", False, str(e))
    ctx = ssl.create_default_context()
    try:
        r = urllib.request.urlopen(
            urllib.request.Request("https://api.binance.com/api/v3/ping"), context=ctx, timeout=10
        )
        check("https", r.status == 200, f"HTTP {r.status}")
        check("ping", r.read().decode() == "{}", "Ping OK")
    except Exception as e:
        err = str(e)
        check("https", False, "GEO_BLOCKED" if "451" in err else err[:80])
    try:
        r = urllib.request.urlopen(
            urllib.request.Request("https://api.binance.com/api/v3/time"), context=ctx, timeout=10
        )
        data = json.loads(r.read())
        drift = int(data.get("serverTime", 0)) - int(time.time() * 1000)
        check("clock", abs(drift) < 5000, f"Drift: {drift}ms")
    except Exception as e:
        check("clock", False, str(e)[:80])
    try:
        r = urllib.request.urlopen(
            urllib.request.Request("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"),
            context=ctx,
            timeout=10,
        )
        p = float(json.loads(r.read())["price"])
        check("ticker", p > 0, f"BTC=${p:,.2f}")
    except Exception as e:
        check("ticker", False, str(e)[:80])
    disk = shutil.disk_usage("/")
    check("disk", disk.free > 500_000_000, f"Free: {disk.free // 1024**3}GB")
    Path("logs").mkdir(exist_ok=True)
    try:
        (Path("logs") / "test.tmp").write_text("ok")
        (Path("logs") / "test.tmp").unlink()
        check("write", True, "logs/ writable")
    except Exception:
        check("write", False, "Permission denied")
    passed = sum(1 for v in RESULTS.values() if v.startswith("PASS"))
    failed = sum(1 for v in RESULTS.values() if v.startswith("FAIL"))
    print(f"\n  RESULTS: {passed} PASS, {failed} FAIL out of {len(RESULTS)}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
