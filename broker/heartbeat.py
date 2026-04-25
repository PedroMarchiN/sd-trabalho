"""
broker/heartbeat.py
─────────────────────────────────────────────────────────────────────────────
Gerencia heartbeats do broker.

Dois papéis
───────────
1. Publicar heartbeat próprio para o cluster (outros brokers sabem que estou vivo)
2. Publicar heartbeat para o Registry (Registry sabe que estou vivo)

Os clientes enviam heartbeat via socket CONTROL (REQ → ROUTER).
O PresenceManager.evict_stale() cuida da expiração de clientes inativos.
"""

import os
import sys
import time
import logging
import threading

import zmq

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


from common.protocol import encode, decode, MSG_CONTROL
from common.channels  import (
    BROKER_ID, REGISTRY_ADDR, HEARTBEAT_INTERVAL,
)

log = logging.getLogger(f"heartbeat.{BROKER_ID}")


class HeartbeatManager:
    """
    Publica heartbeats periódicos do broker.

    Parâmetros
    ──────────
    broker : referência ao Broker pai
    """

    def __init__(self, broker):
        self.broker    = broker
        self.broker_id = broker.broker_id
        self.ctx       = broker.ctx
        self._running  = False

    # ── Ciclo de vida ──────────────────────────────────────────────────────────
    def start(self) -> None:
        self._running = True
        threading.Thread(
            target=self._thread_publish,
            daemon=True,
            name="hb_publish",
        ).start()
        log.debug("HeartbeatManager iniciado (interval=%.1fs)", HEARTBEAT_INTERVAL)

    def stop(self) -> None:
        self._running = False

    # ── Thread ─────────────────────────────────────────────────────────────────
    def _thread_publish(self) -> None:
        """
        A cada HEARTBEAT_INTERVAL segundos:
          • Publica heartbeat no canal de cluster (PUB)
          • Atualiza registro no Registry (REQ/REP)
        """
        while self._running:
            time.sleep(HEARTBEAT_INTERVAL)
            self._publish_cluster_heartbeat()
            self._ping_registry()

    def _publish_cluster_heartbeat(self) -> None:
        """Publica heartbeat via outbox do cluster (thread-safe)."""
        try:
            payload = {
                "action":    "broker_heartbeat",
                "broker_id": self.broker_id,
                "ts":        time.time(),
                "rooms":     self.broker.presence.all_rooms(),
            }
            raw = encode(MSG_CONTROL, self.broker_id, "__cluster__", payload)
            self.broker.cluster.publish_heartbeat(raw)
        except Exception as e:
            log.debug("Erro no heartbeat de cluster: %s", e)

    def _ping_registry(self) -> None:
        """
        Envia um heartbeat ao Registry para manter o registro ativo.
        Usa socket REQ temporário (sem estado entre chamadas).
        """
        sock = self.ctx.socket(zmq.REQ)
        sock.setsockopt(zmq.RCVTIMEO, 1500)
        sock.setsockopt(zmq.LINGER, 0)
        sock.connect(REGISTRY_ADDR)

        try:
            payload = {
                "action":    "heartbeat",
                "broker_id": self.broker_id,
                "ts":        time.time(),
                "clients":   sum(
                    len(v) for v in self.broker.presence.all_rooms().values()
                ),
                # Mapa completo {sala: [membros]} — Registry usa para /who e /rooms
                "rooms":     self.broker.presence.all_rooms(),
            }
            raw = encode(MSG_CONTROL, self.broker_id, "__registry__", payload)
            sock.send(raw)
            sock.recv()   # aguarda ACK (descartado)
        except zmq.ZMQError:
            pass   # Registry indisponível — sem pânico, tentará de novo
        finally:
            sock.close()