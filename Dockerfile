# CrawlRX API + challenge-solving service image. Chromium and its system
# libraries come from the official Playwright base image.
FROM mcr.microsoft.com/playwright/python:v1.56.0-noble

ARG CRAWLRX_VERSION=0.1.0
LABEL org.opencontainers.image.title="CrawlRX" \
      org.opencontainers.image.description="Self-hosted AI web scraper and crawler for local LLMs (Ollama, LM Studio, vLLM) with environment-check detection, stealth Chromium, automatic challenge solving and human hand-off." \
      org.opencontainers.image.version="${CRAWLRX_VERSION}" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.source="https://github.com/LiquidLava1990/crawlrx" \
      org.opencontainers.image.url="https://github.com/LiquidLava1990/crawlrx" \
      org.opencontainers.image.documentation="https://github.com/LiquidLava1990/crawlrx#readme" \
      org.opencontainers.image.authors="CrawlRX contributors"

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CRAWLRX_DATA_DIR=/data \
    CRAWLRX_HOME=/data/config \
    # The built-in solver service listens here; the main API proxies to it.
    FLARESOLVERR_HOST=127.0.0.1 \
    FLARESOLVERR_PORT=8191 \
    CRAWLRX_SOLVER_SERVICE_URL=http://127.0.0.1:8191

COPY pyproject.toml README.md LICENSE THIRD_PARTY_NOTICES.md ./
COPY crawlrx ./crawlrx
# ffmpeg: audio conversion for the reCAPTCHA audio path.
# tesseract: OCR engine for the text-challenge solver.
# libglib2.0-0: runtime lib for the headless OpenCV wheel.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tesseract-ocr libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*
# Pin Playwright to the base image's version so its bundled Chromium is reused.
# The [solver] extra installs the challenge-solving service stack (stealth
# browsers, vision models, OCR engines). The Camoufox runtime is fetched here
# so the container works offline at start.
RUN pip install ".[solver]" "playwright==1.56.*" \
 && python -m playwright install chromium \
 && python -m camoufox fetch \
 && useradd --create-home --uid 10001 crawlrx \
 && mkdir -p /data && chown -R crawlrx /data

USER crawlrx
VOLUME ["/data"]
EXPOSE 7337

# Health goes through the main API on 7337 (/health), which answers only when
# the API is up; the solver service is reachable by the API at /health too.
HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=60s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7337/health', timeout=10).read()"

# Serve the main API on 7337 and the challenge-solving service on 8191
# (start_service binds host/port from FLARESOLVERR_HOST/FLARESOLVERR_PORT).
CMD ["sh", "-c", "python -m crawlrx.solver_service.start_service & exec crawlrx serve --host 0.0.0.0 --port 7337"]
