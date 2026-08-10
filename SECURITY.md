# Security

## Quant Opportunity Engine — Security Architecture

---

## 1. Principle

This software may eventually control real money. Security is not optional. Every design decision must assume the system could be a target.

---

## 2. Live Trading Safety Gate

Live trading is **DISABLED BY DEFAULT**. Two independent conditions must both be true:

1. `MODE=live` (environment variable or `.env`)
2. `LIVE_TRADING_ENABLED=true` (environment variable or `.env`)

Both are checked at order placement time by `ExecutionEngine._ensure_mode_allows()`. If either is false/missing, the system runs in paper mode regardless of any other configuration.

**It must be physically impossible to accidentally enable live trading.**

---

## 3. API Key Management

### Rules (NEVER VIOLATE):

1. **Never hard-code API keys** in source code, config files, or documentation.
2. **Never commit API keys** to version control.
3. **Never log API keys** — the logging system auto-redacts keys matching patterns like `api_key`, `secret`, `password`, `token`.
4. API keys are loaded ONLY from environment variables via `pydantic-settings`.
5. The `.env` file is in `.gitignore` and never committed.

### Exchange API Key Permissions:

For EVERY exchange API key:
- ✅ Trading permission: YES (required)
- ✅ Reading balances/orders: YES (required)
- ❌ Withdrawal permission: **NEVER**
- ❌ Internal transfer: **NEVER**
- ✅ IP whitelisting: RECOMMENDED

### Key Rotation:
- Keys should be rotated every 90 days.
- The system supports hot-reload of configuration for key rotation without restart.

---

## 4. Secrets Redaction

The `_secrets_filter` in `src/core/logging_config.py` automatically redacts any log key containing:
- `api_key`
- `api_secret`
- `secret`
- `password`
- `token`
- `private_key`
- `passphrase`
- `credential`

This applies to structured JSON logs and text logs alike.

---

## 5. Input Validation

All external data is validated before use:
- **Exchange data**: Normalized through Pydantic/dataclass validation.
- **Configuration**: Pydantic models with strict type checking.
- **Dashboard inputs**: FastAPI request validation.
- **Numeric values**: Bounds-checked (no negative quantities, no zero prices, etc.).

---

## 6. Dependency Security

- All dependencies are pinned with minimum versions in `pyproject.toml`.
- `pip audit` or `safety check` should be run in CI/CD to detect known vulnerabilities.
- Dependencies are kept minimal — no unnecessary packages.

---

## 7. Docker Security

- Container runs as **non-root user** (`bot`).
- `HEALTHCHECK` is configured.
- No privileged mode.
- No host network mode in production.
- Read-only root filesystem where possible.
- Secrets are injected at runtime, never baked into the image.

---

## 8. Network Security

- Dashboard binds to `0.0.0.0` for Docker networking but should be behind a reverse proxy with authentication in any non-local deployment.
- Exchange API calls use HTTPS exclusively.
- WebSocket connections use `wss://` only.
- TLS certificate validation is enforced (no `verify=False`).

---

## 9. Audit Trail

Every significant action is logged to the `audit_events` table:
- Order placements (with strategy, signal, opportunity trace).
- Configuration changes.
- Mode switches.
- Kill switch / circuit breaker events.
- Authentication failures.
- System startup/shutdown.

The audit log is **append-only** — rows are never updated or deleted.

---

## 10. Least Privilege

- Database user has only the permissions needed (CRUD on own tables).
- Redis is used only for caching/pubsub — no sensitive data stored.
- File system permissions are restrictive.

---

## 11. Incident Response

If a security issue is suspected:

1. **Trip the kill switch** (`RiskEngine.trip_kill_switch()`).
2. **Cancel all open orders** on all exchanges.
3. **Revoke API keys** on exchange dashboards.
4. **Review audit logs** to understand what happened.
5. **Do NOT restart** the system until the root cause is identified.

---

## 12. Pre-Deployment Checklist

Before ANY deployment (even paper):

- [ ] `.env` file exists and is NOT committed.
- [ ] `MODE=paper` or both live-trading gates verified.
- [ ] API keys have NO withdrawal permissions.
- [ ] All tests pass.
- [ ] `ruff check` passes.
- [ ] `mypy` passes (strict mode).
- [ ] Docker image builds cleanly.
- [ ] Logs are being captured.
- [ ] Alert webhook is configured.
- [ ] Dashboard is accessible but secured.
