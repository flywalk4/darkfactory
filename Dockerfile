FROM python:3.12-slim AS base
RUN apt-get update && apt-get install -y --no-install-recommends git openssh-client ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir --timeout 120 pip==26.2.2
RUN pip install --no-cache-dir --timeout 120 --resume-retries 10 -r requirements.lock
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app

FROM base AS control
# The worker builds project toolchain images with BuildKit through the mounted Docker socket.
COPY --from=docker:28.3.3-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker:28.3.3-cli /usr/local/libexec/docker/cli-plugins/docker-buildx /usr/local/libexec/docker/cli-plugins/docker-buildx
COPY darkfactory ./darkfactory
CMD ["uvicorn", "darkfactory.api:app", "--host", "0.0.0.0", "--port", "8000"]

FROM node:22-slim AS cli
ARG CODEX_VERSION=0.134.0
ARG CLAUDE_VERSION=2.1.304
ARG PLAYWRIGHT_VERSION=1.64.1
ARG PLAYWRIGHT_MCP_VERSION=0.0.83
RUN npm install -g @openai/codex@${CODEX_VERSION} @anthropic-ai/claude-code@${CLAUDE_VERSION} \
    @playwright/test@${PLAYWRIGHT_VERSION} @playwright/mcp@${PLAYWRIGHT_MCP_VERSION} \
 && ln -s "$(find /usr/local/lib/node_modules/@openai/codex -type f -path '*/bin/codex' | head -1)" /usr/local/bin/apply_patch \
 && test -x /usr/local/bin/apply_patch
# Models call apply_patch from the shell; codex answers to that name, so agents stop hunting for a patch tool.

# Everything an agent container needs except the factory's own code. Project toolchains build on this
# image, so a factory deploy does not rebuild their SDK layers.
FROM base AS executor-tools
COPY --from=cli /usr/local /usr/local
ENV NODE_PATH=/usr/local/lib/node_modules PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
# Agents reach for rg, jq and patch first; without them they burn turns on fallbacks.
RUN if ! playwright install-deps chromium > /tmp/playwright-deps.log 2>&1; then \
      cat /tmp/playwright-deps.log; exit 1; \
    fi && apt-get install -y --no-install-recommends ripgrep jq patch procps \
    && rm -rf /var/lib/apt/lists/* /tmp/playwright-deps.log
COPY scripts/install-browser.cjs /tmp/install-browser.cjs
RUN PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT=120000 node /tmp/install-browser.cjs \
    && chmod -R a+rX /opt/playwright \
    && rm /tmp/install-browser.cjs
# Jira MCP (catalog "jira") in its own venv so its dependencies never meet the factory's.
ARG MCP_ATLASSIAN_VERSION=0.23.1
RUN python -m venv /opt/mcp-atlassian \
    && /opt/mcp-atlassian/bin/pip install --no-cache-dir --timeout 120 mcp-atlassian==${MCP_ATLASSIAN_VERSION}
COPY --chmod=755 scripts/mcp-atlassian.sh /usr/local/bin/mcp-atlassian
RUN useradd --uid 1000 --create-home agent && useradd --uid 1001 --no-create-home --shell /usr/sbin/nologin mcp
USER agent
WORKDIR /workspace
ENTRYPOINT ["python", "-m", "darkfactory.runner"]

FROM executor-tools AS executor
COPY darkfactory /app/darkfactory

