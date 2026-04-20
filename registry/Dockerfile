FROM python:3.12-slim

WORKDIR /app

# Registry só precisa de pyzmq + msgpack — sem dependências de sistema
COPY registry/requirements.txt /tmp/req.txt
RUN pip install --no-cache-dir -r /tmp/req.txt

COPY common/   ./common/
COPY registry/ ./registry/

ENV REGISTRY_PORT=5550
ENV BROKER_HOST=0.0.0.0
ENV HEARTBEAT_TIMEOUT=5.0

EXPOSE 5550

CMD ["python", "-m", "registry.registry"]