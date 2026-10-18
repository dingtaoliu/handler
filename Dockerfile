# Handler runtime image (handler-rt serve), built for Heroku container deploys.
# Heroku dynos are linux/amd64; build with --platform linux/amd64 on ARM hosts.
FROM python:3.12-slim

# Codex CLI, pinned to an official openai/codex release (static x86_64 musl).
ARG CODEX_VERSION=0.162.0
ARG CODEX_SHA256=8daf67f6261161aa5939d8d42a516032d760406216779d7ce6040f242140ff73
# Codex runs every tool (shell and MCP) through this helper; without it all
# tool calls fail before they start. It must sit next to the codex binary.
ARG CODE_MODE_HOST_SHA256=dee8bf3df637c68e1495b928d643b76d4ad44f94e4f0ed79fe8482fa79e23d97
ARG UV_VERSION=0.10.10

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=0 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    HOME=/home/handler \
    HANDLER_RT_DATA_DIR=/tmp/handler

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git bubblewrap \
    && curl -fsSL -o /tmp/codex.tar.gz \
        "https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/codex-x86_64-unknown-linux-musl.tar.gz" \
    && echo "${CODEX_SHA256}  /tmp/codex.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/codex.tar.gz -C /tmp \
    && install -m 0755 /tmp/codex-x86_64-unknown-linux-musl /usr/local/bin/codex \
    && rm -f /tmp/codex.tar.gz /tmp/codex-x86_64-unknown-linux-musl \
    && curl -fsSL -o /tmp/cmh.tar.gz \
        "https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/codex-code-mode-host-x86_64-unknown-linux-musl.tar.gz" \
    && echo "${CODE_MODE_HOST_SHA256}  /tmp/cmh.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/cmh.tar.gz -C /tmp \
    && install -m 0755 /tmp/codex-code-mode-host-x86_64-unknown-linux-musl /usr/local/bin/codex-code-mode-host \
    && rm -f /tmp/cmh.tar.gz /tmp/codex-code-mode-host-x86_64-unknown-linux-musl \
    && codex --version \
    && apt-get purge -y curl && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "uv==${UV_VERSION}"

WORKDIR /app

# Dependencies first so this layer is cached until the lockfile changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY deploy ./deploy
RUN uv sync --frozen --no-dev \
    && chmod +x deploy/entrypoint.sh \
    && useradd --create-home --uid 1000 handler \
    && mkdir -p /tmp/handler && chown handler /tmp/handler

USER handler
EXPOSE 8100
CMD ["deploy/entrypoint.sh"]
