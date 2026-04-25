#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# run_client.sh — Instala dependências (se necessário) e inicia o cliente.
#
# Uso:
#   ./run_client.sh --id alice --room A
#   ./run_client.sh --id bob   --room B
#   ./run_client.sh            # ID gerado automaticamente
#
# Pré-requisito: registry e brokers rodando no Docker
#   docker compose up --build registry broker-1 broker-2 broker-3
# ─────────────────────────────────────────────────────────────────────────────

set -e
cd "$(dirname "$0")"

# ── Cria virtualenv e instala dependências se ainda não existir ───────────────
if [ ! -f ".venv/bin/activate" ]; then
    echo "==> Criando ambiente virtual (.venv)..."
    python3 -m venv .venv
fi

source .venv/bin/activate

# Verifica se pyzmq já está instalado (proxy para "deps ok")
if ! python -c "import zmq" 2>/dev/null; then
    echo "==> Instalando dependências Python..."
    pip install --upgrade pip -q
    pip install pyzmq msgpack opencv-python-headless numpy pyaudio
    echo "==> Dependências instaladas."
fi

# ── Configura Qt/OpenCV para evitar warnings e crash no Wayland ───────────────
export QT_LOGGING_RULES="*.debug=false;qt.qpa.*=false"
export OPENCV_LOG_LEVEL=ERROR
export QT_QPA_PLATFORM=xcb      # opencv-python só tem plugin xcb
export XDG_SESSION_TYPE=x11     # suprime aviso "Ignoring XDG_SESSION_TYPE=wayland"

# ── Inicia o cliente ───────────────────────────────────────────────────────────
REGISTRY_HOST=localhost \
REGISTRY_PORT=5550 \
HEARTBEAT_INTERVAL=2.0 \
HEARTBEAT_TIMEOUT=8.0 \
python -m client.client "$@"