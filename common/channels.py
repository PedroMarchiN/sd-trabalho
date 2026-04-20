"""
common/channels.py
Constantes de portas e nomes de canais.
Lidos via variáveis de ambiente (com fallback para defaults).
"""
import os


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


# ── Registry ───────────────────────────────────────────────────────────────────
REGISTRY_HOST = _env("REGISTRY_HOST", "registry")
REGISTRY_PORT = int(_env("REGISTRY_PORT", "5550"))
REGISTRY_ADDR = f"tcp://{REGISTRY_HOST}:{REGISTRY_PORT}"

# ── Broker (portas base; cada instância recebe BROKER_BASE_PORT via env) ───────
BROKER_BASE_PORT   = int(_env("BROKER_BASE_PORT", "5555"))

# Offsets fixos a partir da porta base
OFFSET_FRONTEND    = 0   # XSUB  — recebe dos clientes
OFFSET_BACKEND     = 1   # XPUB  — envia aos clientes
OFFSET_CONTROL     = 2   # ROUTER — presença / controle
OFFSET_CLUSTER_PUB = 3   # PUB   — publica para outros brokers
OFFSET_CLUSTER_SUB = 4   # SUB   — assina outros brokers

def broker_ports(base: int = BROKER_BASE_PORT) -> dict:
    return {
        "frontend":    base + OFFSET_FRONTEND,
        "backend":     base + OFFSET_BACKEND,
        "control":     base + OFFSET_CONTROL,
        "cluster_pub": base + OFFSET_CLUSTER_PUB,
        "cluster_sub": base + OFFSET_CLUSTER_SUB,
    }

# ── Salas disponíveis ──────────────────────────────────────────────────────────
ROOMS = [chr(c) for c in range(ord("A"), ord("K") + 1)]   # A … K

# ── HWM (High Water Mark) por tipo de mídia ────────────────────────────────────
# Controla o comportamento de descarte quando a fila enche
HWM = {
    "text":  1000,   # garantia de entrega — fila grande
    "audio":    5,   # baixa latência      — descarta se lotado
    "video":    2,   # taxa adaptativa     — máx 2 frames na fila
}

# ── Heartbeat ──────────────────────────────────────────────────────────────────
HEARTBEAT_INTERVAL  = float(_env("HEARTBEAT_INTERVAL",  "1.0"))   # segundos
HEARTBEAT_TIMEOUT   = float(_env("HEARTBEAT_TIMEOUT",   "5.0"))   # sem resposta → falha
HEARTBEAT_RETRIES   = int(_env("HEARTBEAT_RETRIES",     "3"))

# ── Broker ID ──────────────────────────────────────────────────────────────────
BROKER_ID = _env("BROKER_ID", "broker-1")
BROKER_HOST = _env("BROKER_HOST", "0.0.0.0")