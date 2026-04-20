FROM python:3.12-slim

WORKDIR /app

# Dependências de sistema:
#   portaudio19-dev  — pyaudio (Fase 2: áudio)
#   libgl1           — opencv (Fase 3: vídeo)
#   gcc / libc-dev   — compilação de extensões C
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libc-dev \
        portaudio19-dev \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY client/requirements.txt /tmp/req.txt
RUN pip install --no-cache-dir -r /tmp/req.txt

COPY common/ ./common/
COPY client/  ./client/

ENV REGISTRY_HOST=registry
ENV REGISTRY_PORT=5550
ENV HEARTBEAT_INTERVAL=1.0
ENV HEARTBEAT_TIMEOUT=5.0

# Modo interativo: executar com  docker run -it ... python -m client.client --id alice
CMD ["python", "-m", "client.client", "--help"]