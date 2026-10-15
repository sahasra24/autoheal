# syntax=docker/dockerfile:1.7
# Build from the repository root:  docker build -f services/gateway/Dockerfile -t autoheal-gateway .

########## builder: resolve and compile dependencies into an isolated venv ##########
FROM python:3.12-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

# Dependencies first: this layer is cached until requirements change.
COPY services/gateway/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

COPY libs/common /tmp/common
RUN pip install --no-deps /tmp/common

########## runtime: minimal image, no build tooling, non-root ##########
FROM python:3.12-slim AS runtime
ARG VERSION=dev
LABEL org.opencontainers.image.title="autoheal-gateway"       org.opencontainers.image.version="${VERSION}"       org.opencontainers.image.source="https://github.com/sahasra24/autoheal"

ENV PATH=/opt/venv/bin:$PATH     PYTHONDONTWRITEBYTECODE=1     PYTHONUNBUFFERED=1     PORT=8000

RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app --no-create-home app
COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY --chown=10001:10001 services/gateway/autoheal_gateway ./autoheal_gateway

USER 10001:10001
EXPOSE 8000

# Used by docker compose; Kubernetes uses its own probes instead.
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=3   CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=2).status == 200 else 1)"

# exec form: uvicorn is PID 1 and receives SIGTERM directly for graceful shutdown.
CMD ["python", "-m", "uvicorn", "--factory", "autoheal_gateway.main:create_app",      "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--timeout-graceful-shutdown", "25"]
