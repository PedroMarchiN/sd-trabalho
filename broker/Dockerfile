FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc libc-dev \
    && rm -rf /var/lib/apt/lists/*

# Broker só precisa de pyzmq + msgpack
RUN pip install --no-cache-dir pyzmq>=25.1.2 msgpack>=1.0.7

COPY common/ ./common/
COPY broker/  ./broker/

ENV BROKER_ID=broker-1
ENV BROKER_HOST=0.0.0.0
ENV BROKER_ADVERTISE_HOST=broker-1
ENV BROKER_BASE_PORT=5555
ENV REGISTRY_HOST=registry
ENV REGISTRY_PORT=5550
ENV HEARTBEAT_INTERVAL=2.0
ENV HEARTBEAT_TIMEOUT=8.0

EXPOSE 5555 5556 5557 5558 5559

CMD ["python", "-m", "broker.broker"]