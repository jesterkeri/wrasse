# The hosted demo, built so the image carries every fact the running service has to prove.
#
# Two stages, and the first one is not optional. `_require_deployment_identity` compares the
# code at the configured address against the runtime bytecode *this build compiled*, so a
# container without the compiled artifact cannot send a transaction at all. `contracts/out` is
# gitignored, correctly, because a committed build output is a claim nobody checked. So the
# image compiles it, and the check keeps meaning what it says.

# Pinned, not `stable`. The runtime refuses to send value to a contract whose code does not
# match the artifact this build compiled, and two Foundry releases do not necessarily produce
# byte-identical output from the same source and the same pinned solc. `stable` moved, this
# image compiled something with a different hash, and the deployed service quoted perfectly and
# then refused every settlement. This is the version that produced the reviewed artifact.
FROM ghcr.io/foundry-rs/foundry:v1.8.1 AS contracts
# The upstream image drops to an unprivileged user, and `WORKDIR` creates its directory owned
# by root, so `forge` compiled fine and then could not write `contracts/out`. Root for the
# length of one compile, in a stage whose only output is copied into the runtime image.
USER root
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

# And prove it matches the contract on Base, here, where a mismatch is a red build log rather
# than a deployed service that quotes correctly and refuses every settlement. That is exactly
# what shipped once: the check that catches this lives on the signing path, so nothing noticed
# until a real settlement was attempted against the deployment. It uses the project's own
# hashing rather than a second implementation, so a drift between them cannot hide here.
RUN python -c "\
import json; \
from wrasse import escrow; \
built = escrow.artifact_runtime_hash(); \
recorded = json.load(open('deployments/base-sepolia.json'))['runtime_bytecode_hash']; \
print('artifact', built); \
print('deployed', recorded); \
raise SystemExit(0 if built == recorded else \
  'this image cannot reproduce the reviewed build, so the service it produces would quote ' \
  'correctly and refuse every settlement. Pin the Foundry version that produced the artifact.')"


COPY deploy/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# `gosu` exists so the entrypoint can start as root, take ownership of the mounted volume, and
# then drop for the whole life of the service. The platform mounts the volume owned by root and
# nothing in the image can change that in advance, because the mount replaces whatever the image
# had at that path.
RUN apt-get update \
 && apt-get install -y --no-install-recommends gosu \
 && rm -rf /var/lib/apt/lists/*

# Secrets are materialised here at boot, never into the image and never onto the volume. The
# volume is backed up; a keystore on it would be backed up with it.
RUN mkdir -p /run/wrasse && chmod 700 /run/wrasse

# Not root, but the drop happens in the entrypoint rather than here. The service decrypts two
# keystores, and a process that can also rewrite its own code is a larger blast radius than it
# needs; the entrypoint needs one root-only act first, which is taking the volume.
RUN useradd --create-home --uid 10001 wrasse \
 && chown -R wrasse:wrasse /app /run/wrasse

EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
