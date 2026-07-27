# syntax=docker/dockerfile:1.6
# ─────────────────────────────────────────────────────────────────────────────
# MapLead — production-ready container
# Multi-stage build keeps the final image lean.
# ─────────────────────────────────────────────────────────────────────────────

# ── Stage 1: install Playwright browsers (heavy, can be cached separately) ──
FROM mcr.microsoft.com/playwright/python:v1.42.0-jammy AS browsers

# Pre-install only chromium (and its system deps) — we don't need firefox/webkit
RUN playwright install --with-deps chromium

# ── Stage 2: the actual app ────────────────────────────────────────────────
FROM python:3.11-slim

# System libs needed by Chromium at runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libx11-xcb1 \
    libxcomposite1 libxcursor1 libxdamage1 libxfixes3 \
    libxi6 libxrandr2 libgbm1 libasound2 libpangocairo-1.0-0 \
    libpango-1.0-0 libgdk-pixbuf2.0-0 libgtk-3-0 libdrm2 \
    curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user for runtime safety
RUN useradd --create-home --shell /bin/bash --uid 1001 maplead
WORKDIR /app

# Install Python deps first (better layer caching)
COPY --chown=maplead:maplead requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY --chown=maplead:maplead app.py scraper.py utils.py api_backends.py ./
COPY --chown=maplead:maplead .streamlit ./.streamlit
COPY --chown=maplead:maplead README.md LICENSE ./

# Copy pre-installed Playwright browsers from stage 1
COPY --from=browsers --chown=maplead:maplead /ms-playwright /home/maplead/.cache/ms-playwright

USER maplead

# Streamlit config
ENV STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_SERVER_PORT=8501 \
    STREAMLIT_SERVER_ADDRESS=0.0.0.0 \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/home/maplead/.cache/ms-playwright

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl --fail http://localhost:8501/_stcore/health || exit 1

ENTRYPOINT ["streamlit", "run", "app.py", \
            "--server.headless=true", \
            "--server.address=0.0.0.0", \
            "--server.port=8501"]
