FROM python:3.12-slim

WORKDIR /app

# Dependências de sistema para opencv e pyaudio
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libc-dev \
        libgl1 \
        libglib2.0-0 \
        libsm6 \
        libxext6 \
        libxrender1 \
        libportaudio2 \
        portaudio19-dev \
        libasound2-dev \
        libx11-xcb1 \
        libxcb-icccm4 \
        libxcb-image0 \
        libxcb-keysyms1 \
        libxcb-randr0 \
        libxcb-render-util0 \
        libxcb-xinerama0 \
        libxcb-xkb1 \
        libxkbcommon-x11-0 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
        pyzmq>=25.1.2 \
        msgpack>=1.0.7 \
        opencv-python \
        pyaudio \
        numpy

COPY common/ ./common/
COPY client/  ./client/

ENV REGISTRY_HOST=registry
ENV REGISTRY_PORT=5550
ENV HEARTBEAT_INTERVAL=2.0
ENV HEARTBEAT_TIMEOUT=8.0
ENV QT_LOGGING_RULES="*.debug=false;qt.qpa.*=false"
ENV OPENCV_LOG_LEVEL=ERROR
ENV QT_QPA_PLATFORM=xcb
ENV XDG_SESSION_TYPE=x11

CMD ["python", "-m", "client.client"]