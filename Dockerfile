FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.title="Quant Opportunity Engine"
LABEL org.opencontainers.image.description="Multi-Market / Multi-Strategy Algorithmic Trading System"

# Security: run as non-root
RUN groupadd --system bot && useradd --system --gid bot --create-home bot

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY pyproject.toml .
RUN pip install --no-cache-dir -e ".[dev]" && pip cache purge

# Copy source
COPY --chown=bot:bot . .

# Switch to non-root user
USER bot

# Default: paper mode only
ENV MODE=paper
ENV LIVE_TRADING_ENABLED=false
ENV DASHBOARD_HOST=0.0.0.0
ENV DASHBOARD_PORT=8080

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8080/health')"

CMD ["python", "-m", "src.paper.engine"]
