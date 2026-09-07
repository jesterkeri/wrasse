# The hosted demo, built so the image carries every fact the running service has to prove.
#
# Two stages, and the first one is not optional. `_require_deployment_identity` compares the
# code at the configured address against the runtime bytecode *this build compiled*, so a
# container without the compiled artifact cannot send a transaction at all. `contracts/out` is
# gitignored, correctly, because a committed build output is a claim nobody checked. So the
# image compiles it, and the check keeps meaning what it says.

FROM ghcr.io/foundry-rs/foundry:stable AS contracts
WORKDIR /src
COPY contracts/ ./contracts/
RUN cd contracts && forge build --sizes


FROM python:3.12-slim AS runtime

# `uv` resolves from the committed lock file, so the image installs the versions this build
# was tested against rather than whatever is newest on the day it is deployed.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra service --no-install-project

COPY wrasse/ ./wrasse/
COPY personas/ ./personas/
COPY deployments/ ./deployments/
COPY web/ ./web/
COPY docs/ ./docs/
COPY README.md ./
RUN uv sync --frozen --no-dev --extra service

# The compiled artifact, from the stage that actually compiled it.
COPY --from=contracts /src/contracts/out/ ./contracts/out/

COPY deploy/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Secrets are materialised here at boot, never into the image and never onto the volume. The
# volume is backed up; a keystore on it would be backed up with it.
RUN mkdir -p /run/wrasse && chmod 700 /run/wrasse

# Not root. The service decrypts two keystores, and a process that can also rewrite its own
# code is a larger blast radius than it needs.
RUN useradd --create-home --uid 10001 wrasse \
 && chown -R wrasse:wrasse /app /run/wrasse
USER wrasse

EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
