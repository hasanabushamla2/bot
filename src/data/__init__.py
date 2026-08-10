"""Real-Time Market Data Engine.

Components:
- engine.py:      WebSocket-first ingestion with REST fallback (existing)
- normalization.py: Strict canonical event models
- order_book.py:  Deterministic local order book engine
- rate_limiter.py: Token-bucket rate limiting
- feed_health.py: Stale-data detection and feed monitoring
- event_bus.py:   Async non-blocking event distribution
- metrics.py:     Latency and throughput metrics
- historical.py:  Historical data ingestion foundation
"""
