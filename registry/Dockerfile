FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc libc-dev \
    && rm -rf /var/lib/apt/lists/*

# Registry só precisa de pyzmq + msgpack
RUN pip install --no-cache-dir pyzmq>=25.1.2 msgpack>=1.0.7

COPY common/   ./common/
COPY registry/ ./registry/

ENV REGISTRY_PORT=5550
ENV BROKER_HOST=0.0.0.0
ENV HEARTBEAT_TIMEOUT=8.0

EXPOSE 5550

CMD ["python", "-m", "registry.registry"]