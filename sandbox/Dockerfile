# Per-agent sandbox image: Debian + Python + Playwright Chromium + common CLI tools.
# Build: docker build -t troupe-sandbox:latest sandbox/
FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        bash ca-certificates curl wget git jq ripgrep unzip zip less nano procps \
        poppler-utils pandoc sqlite3 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install playwright==1.55.0 requests beautifulsoup4 pandas openpyxl pypdf python-docx \
    && playwright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright \
    && rm -rf /var/lib/apt/lists/* /root/.cache

COPY agent_browser.py /opt/troupe/agent_browser.py
RUN printf '#!/bin/sh\nexec python3 /opt/troupe/agent_browser.py "$@"\n' > /usr/local/bin/troupe-browser \
    && chmod 755 /usr/local/bin/troupe-browser /opt/troupe/agent_browser.py

WORKDIR /workspace
CMD ["sleep", "infinity"]
