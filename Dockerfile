# lyra-browser for Cloudflare Containers (linux/amd64 only).
# Chrome stable + the Python MCP server over Streamable HTTP. Auth lives in the
# Worker in front (deploy/cloudflare); this image binds all interfaces and
# must never be published directly.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src

# Playwright's own installer pulls Chrome stable and the shared libraries it
# needs; the browser lands in /opt/google/chrome, readable by any user.
RUN pip install . \
    && python -m playwright install --with-deps chrome \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 lyra \
    && mkdir /data \
    && chown lyra:lyra /data
USER lyra

# remote: headless, screenshots returned as MCP images. No bundled-Chromium
# fallback — a missing Chrome must fail loudly, not run a different browser.
ENV LYRA_BROWSER_CLIENT=remote \
    LYRA_BROWSER_HEADLESS=true \
    LYRA_BROWSER_DATA_DIR=/data \
    LYRA_BROWSER_CHANNEL=chrome \
    LYRA_BROWSER_ALLOW_BUNDLED=false \
    LYRA_BROWSER_DRIVER=playwright \
    LYRA_BROWSER_AUDIT_STDERR=true

EXPOSE 8765
ENTRYPOINT ["lyra-browser", "--http", "--host", "0.0.0.0", "--port", "8765"]
