# The one Citadel image: API, worker, sandbox and the setup step all run from it, with
# a different command. Everything is fetched HERE, at build time; nothing a running
# container does needs the internet (packages/sovereignty/AGENTS.md, "Build-time egress
# counts too" -- and then the running system is denied egress anyway).
#
#   docker compose -f ops/compose/docker-compose.yml build

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      postgresql-client tesseract-ocr tesseract-ocr-eng nftables ca-certificates fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 citadel && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin citadel \
 && groupadd --gid 10002 sandbox && useradd --uid 10002 --gid 10002 --no-create-home --shell /usr/sbin/nologin sandbox

WORKDIR /app
COPY ops/compose/requirements.txt /app/ops/compose/requirements.txt
RUN pip install -r /app/ops/compose/requirements.txt

COPY packages /app/packages
COPY services /app/services
COPY registry /app/registry
COPY web /app/web
COPY ops /app/ops
# A Windows checkout may have given the scripts CRLF endings; bash inside Linux cannot run those.
RUN sed -i 's/\r$//' /app/ops/compose/*.sh /app/ops/nftables/*.nft && chmod 0755 /app/ops/compose/*.sh

ENV PYTHONPATH=/app/packages/contracts/src:/app/packages/platform/src:/app/packages/gateway/src:/app/packages/knowledge/src:/app/packages/memory/src:/app/packages/deliverables/src:/app/packages/tools/src:/app/packages/runtime/src:/app/packages/sovereignty/src:/app/services/api/src:/app/services/worker/src:/app/services/sandbox/src \
    CITADEL_REGISTRY_DIR=/app/registry \
    CITADEL_WEB_DIR=/app/web/src \
    CITADEL_DATA_DIR=/data \
    CITADEL_KEYS_DIR=/keys \
    CITADEL_PROFILE=demo-local

CMD ["python", "-m", "citadel_api"]
